"""Decision engine: score every legal action, execute the best one, record why."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace

from . import economy as econ
from . import farming, refining, world
from .combat import CombatMemory, select_monster
from .config import Config
from .guardrails import GuardrailViolation
from .transport import ApiError

# Monsters observed in live traffic. Extend as more are confirmed.
MONSTER_CATALOG = [
    "forestspider_lvl1_1",
    "forestspider_lvl1_2",
    "aerial_lvl4_1",
]

# Expected drops per battle, by monster. Values are quantities, priced from the
# live market at scoring time rather than assumed to be worth anything.
EXPECTED_DROPS = {
    "forestspider_lvl1_1": {"spiderfang": 1.5},
    "forestspider_lvl1_2": {"spiderfang": 1.5},
    "aerial_lvl4_1": {"aerial_feather": 1.2},
}

PRODUCTION_LOCATIONS = {"city_2"}
BATTLE_LOCATIONS = {"farm_3", "wildland_1"}

# Never start a fight below this health ratio, whatever the score says.
BATTLE_MIN_HEALTH_RATIO = 0.45

# Take a free energy refill only once the bar has drained this far, so a daily
# use is never spent restoring a handful of points.
ENERGY_REFILL_RATIO = 0.35


@dataclass
class _GoldBudget:
    """Presents a player's spendable gold, keeping the configured reserve untouched.

    Farming costs gold, and an engine that happily spends the balance to zero
    leaves the account unable to pay for anything else.
    """

    state: object
    gold: int

    @property
    def energy(self) -> int:
        return self.state.energy


@dataclass
class Decision:
    wallet_id: str
    action: str = "idle"
    params: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    reason: str = ""
    considered: list = field(default_factory=list)
    error: str = ""
    dry_run: bool = False

    def rationale_lines(self) -> list[str]:
        """Human-readable explanation, surfaced by the Telegram 'why' button."""
        lines = [f"Chose: {self.action} — {self.reason}"]
        for candidate in self.considered:
            score = ("∞" if candidate.score == econ.INFINITE
                     else f"{candidate.score:,.0f} g/h")
            lines.append(
                f"  {candidate.action:16s} {score:>14s}  "
                f"({candidate.energy_cost}en, {candidate.duration_seconds / 60:.0f}m) "
                f"{candidate.reason}")
        return lines


@dataclass
class Orchestrator:
    config: Config
    api: object
    economy: econ.Economy = field(default_factory=econ.Economy)
    combat: CombatMemory = field(default_factory=CombatMemory)
    rng: random.Random = field(default_factory=random.Random)

    def decide_and_act(self, wallet: dict, session, state, market=None,
                       holdings=None, task_status=None) -> Decision:
        decision = Decision(wallet_id=wallet["id"], dry_run=self.config.dry_run)

        try:
            candidates = self.build_candidates(
                state, market, holdings, task_status=task_status)
            decision.considered = candidates
            if not candidates:
                decision.action = "idle"
                decision.reason = self._idle_reason(state)
                return decision

            chosen = candidates[0]
            decision.action = chosen.action
            decision.params = chosen.params
            decision.reason = chosen.reason

            if self.config.dry_run:
                decision.result = {"dry_run": True}
                return decision

            decision.result = self.execute(session, chosen, state)
        except GuardrailViolation as exc:
            decision.action = "blocked"
            decision.error = str(exc)
        except ApiError as exc:
            # A benign rejection means the server already did the thing; it is not
            # a failure and must not count toward the circuit breaker.
            if exc.is_benign:
                decision.result = {"already_done": str(exc)}
            else:
                decision.error = f"{exc.status_code or 'ERROR'}: {exc}"
        return decision

    # --- candidate generation -------------------------------------------
    def build_candidates(self, state, market=None, holdings=None,
                         include_travel: bool = True, task_status=None) -> list:
        # Free value first: an expired activity is reward already earned, and
        # unclaimed level rewards cost nothing.
        if state.activity is not None and state.activity.is_expired:
            return [econ.free_candidate(
                "finishActivity", {},
                f"{state.activity.type} finished, reward waiting")]

        if state.is_busy:
            return []

        unclaimed = state.unclaimed_levels()
        if unclaimed:
            return [econ.free_candidate(
                "claimInitialReward", {"level": unclaimed[0]},
                f"level {unclaimed[0]} reward unclaimed")]

        if state.attribute_points > 0:
            return [econ.free_candidate(
                "spendAttributePoints",
                {"targetType": "attribute", "targetId": "vitality", "amount": 1},
                f"{state.attribute_points} attribute point(s) unspent")]

        # Three free refills a day, and energy gates almost everything. Only worth
        # taking once the bar has drained enough that a refill is not wasted.
        if (state.free_refills_left() > 0
                and state.energy <= state.max_energy * ENERGY_REFILL_RATIO):
            return [econ.energy_refill_candidate(state)]

        # A finished hunt task is gold sitting there; accepting the next one is free.
        if task_status is not None and task_status.eligible:
            if task_status.can_claim:
                return [econ.free_candidate(
                    "claimTaskReward", {},
                    f"task reward of {task_status.task.gold_reward:,} gold ready")]
            if task_status.can_accept:
                return [econ.free_candidate(
                    "acceptTask", {},
                    f"next hunt task available "
                    f"({task_status.completed_count} completed)")]

        candidates = []
        stale = market is None or not market.is_fresh(self.config.market_ttl_seconds)

        needs_rest = (state.health_ratio < self.config.rest_hp_ratio
                      or state.mana_ratio < self.config.rest_mp_ratio)
        if needs_rest:
            candidates.append(econ.relax_candidate(state))

        if not self.config.enabled:
            return econ.rank([self.economy.score_action(c, state.energy, state.max_energy)
                              for c in candidates])

        if state.location_id in PRODUCTION_LOCATIONS and state.energy >= econ.PRODUCTION_ENERGY:
            candidates.append(econ.production_candidate(cycles=1))

        # Refining. This is what makes gathering worth doing at all: raw materials
        # carry no bids, refined goods do.
        workshop = refining.workshop_at(state.location_id)
        if workshop is not None:
            spendable = max(0, state.gold - self.config.gold_reserve)
            recipe = refining.best_recipe(
                workshop, state.level, state.grade, holdings or {}, spendable, market)
            if recipe is not None:
                candidates.append(econ.refining_candidate(recipe, market, self.config))
            else:
                catalyst = self._catalyst_candidate(
                    workshop, state, holdings or {}, spendable, market, stale)
                if catalyst is not None:
                    candidates.append(catalyst)

        # Gathering. Reverse-engineered from the frontend; the bot previously had
        # no access to this economy at all.
        if state.location_id in farming.FARM_LOCATIONS:
            resource = farming.best_resource(
                state.location_id, state.level, state.grade, market)
            if resource is not None:
                spendable = max(0, state.gold - self.config.gold_reserve)
                budget_state = _GoldBudget(state, spendable)
                candidates.extend(econ.farming_candidates(
                    resource, budget_state, market, self.config))

        if (state.location_id in BATTLE_LOCATIONS
                and state.energy >= econ.BATTLE_ENERGY
                and state.health_ratio >= BATTLE_MIN_HEALTH_RATIO):
            monster = select_monster(MONSTER_CATALOG, state.level, state.health_ratio)
            if monster:
                drop_values = {}
                if not stale:
                    for item in EXPECTED_DROPS.get(monster, {}):
                        bid = market.best_bid(item)
                        if bid:
                            drop_values[item] = bid
                candidates.append(econ.battle_candidate(
                    monster, self.economy,
                    drop_values=drop_values,
                    expected_drops=EXPECTED_DROPS.get(monster),
                    market_stale=stale,
                ))

        # Resting is worth considering even at decent health when nothing else is
        # affordable, because it converts idle time into future capacity.
        if not candidates and state.health < state.max_health:
            candidates.append(econ.relax_candidate(state))

        scored = [self.economy.score_action(c, state.energy, state.max_energy)
                  for c in candidates]

        # Never pay more than an action returns. Rest is exempt: when health is
        # low it is a prerequisite, not a trade.
        profitable = [c for c in scored
                      if c.score > 0 or (c.action == "startRelax" and needs_rest)]

        if include_travel and self.config.auto_travel:
            local_best = max((c.score for c in profitable), default=0.0)
            relocation = self._travel_candidate(
                state, market, holdings, local_best)
            if relocation is not None:
                profitable.append(relocation)

        return econ.rank(profitable)

    def _travel_candidate(self, state, market, holdings, local_best: float):
        """Consider moving to wherever the next action is worth more.

        Gathering happens at farm zones and refining in city workshops, so a
        wallet that never moves is limited to whatever its current location
        offers. Each destination is valued by its best action amortised over the
        travel time it costs to get there, which is what stops the engine
        crossing the map for a marginal gain.
        """
        best = None
        for destination in world.economic_locations():
            if destination == state.location_id:
                continue

            seconds = world.travel_seconds(state.location_id, destination)
            if seconds == float("inf"):
                continue

            elsewhere = replace(state, location_id=destination, activity=None)
            remote = self.build_candidates(
                elsewhere, market, holdings, include_travel=False)
            if not remote:
                continue

            target = remote[0]
            total_hours = (seconds + target.duration_seconds) / 3600.0
            if total_hours <= 0:
                continue
            amortised = (target.gold_equivalent - target.gold_cost) / total_hours

            if best is None or amortised > best[0]:
                best = (amortised, destination, target, seconds)

        if best is None:
            return None

        amortised, destination, target, seconds = best
        # Only move when the payoff clearly beats staying put; travel time is
        # dead time and a marginal gain does not justify it.
        if amortised <= local_best * self.config.travel_margin:
            return None

        return econ.ActionScore(
            action="startTravel",
            params={"destinationId": destination},
            gold_equivalent=target.gold_equivalent - target.gold_cost,
            energy_cost=0,
            duration_seconds=seconds,
            score=amortised,
            reason=(f"travel {int(seconds) // 60}m to {world.name_of(destination)} "
                    f"for {target.action} ({amortised:,.0f} g/h after travel)"),
            degraded=target.degraded,
        )

    def _catalyst_candidate(self, workshop, state, holdings: dict, spendable: int,
                            market, stale: bool):
        """Buy catalysts when they are the only thing blocking a profitable run.

        Only worth doing when the raw material is already in hand and the output
        has a real bid — otherwise this is spending gold on an item with no resale
        value for a run that may never pay.
        """
        if market is None or stale:
            return None

        best = None
        for tier in range(1, len(workshop.items) + 1):
            if state.grade < tier:
                break
            item_id = workshop.output_for_tier(tier)
            bid = market.best_bid(item_id) or 0.0
            if bid <= 0:
                continue

            reachable = refining.cycles_if_catalyst_bought(
                workshop, item_id, holdings, spendable)
            have = int(holdings.get(workshop.catalyst_for(tier), 0))
            needed = reachable - have
            if reachable <= 0 or needed <= 0:
                continue

            quantity = refining.affordable_catalysts(tier, spendable, needed)
            if quantity <= 0:
                continue

            # Value of the run this purchase makes possible, net of its own costs.
            unlocked = (bid * reachable
                        - refining.GOLD_PER_CYCLE.get(tier, 0) * reachable)
            if best is None or unlocked > best[0]:
                best = (unlocked, tier, quantity)

        if best is None:
            return None
        unlocked, tier, quantity = best
        return econ.catalyst_candidate(workshop, tier, quantity, unlocked, stale)

    def _idle_reason(self, state) -> str:
        if state.is_busy and state.activity:
            remaining = state.activity.seconds_remaining()
            return f"{state.activity.type} running, {remaining / 60:.0f}m left"
        if not self.config.enabled:
            return "SLCW_ENABLED is false — claims only"
        if state.energy < econ.BATTLE_ENERGY:
            return "out of energy"
        return f"no profitable action at {state.location_id or 'unknown location'}"

    # --- execution -------------------------------------------------------
    def execute(self, session, candidate, state) -> dict:
        action = candidate.action
        if action == "finishActivity":
            return self.api.finish_activity(session)
        if action == "claimInitialReward":
            return self.api.claim_initial_reward(session, candidate.params["level"])
        if action == "spendAttributePoints":
            return self.api.spend_attribute_points(
                session, candidate.params["targetType"],
                candidate.params["targetId"], candidate.params["amount"])
        if action == "startRelax":
            return self.api.start_relax(session)
        if action == "startProduction":
            return self.api.start_production(session, candidate.params.get("cycles", 1))
        if action == "startFarming":
            return self.api.start_farming(session, candidate.params)
        if action == "startRefining":
            return self.api.start_refining(session, candidate.params)
        if action == "refillEnergyFree":
            return self.api.refill_energy_free(session)
        if action == "purchaseCraftingItem":
            return self.api.purchase_crafting_item(session, candidate.params)
        if action == "startTravel":
            return self.api.start_travel(session, candidate.params["destinationId"])
        if action == "acceptTask":
            return self.api.accept_task(session)
        if action == "claimTaskReward":
            return self.api.claim_task_reward(session)
        if action == "battle":
            return self.run_battle(session, candidate.params["monsterId"])
        raise GuardrailViolation(f"orchestrator has no executor for {action!r}")

    def run_battle(self, session, monster_id: str) -> dict:
        """Fight to a conclusion, then always settle the activity.

        The previous implementation raised on hitting the turn cap without calling
        finishActivity, which left the battle open server-side and burned the energy
        for nothing. Here the settle step runs on every exit path.
        """
        started = self.api.start_battle(session, monster_id)
        battle_id = started.get("battleId")
        if not battle_id:
            raise ApiError("startBattle returned no battleId")

        turns = 0
        finished = False
        for _ in range(self.config.battle_max_turns):
            time.sleep(self.rng.uniform(1.8, 3.6))
            attack, defense = self.combat.choose_zones(monster_id, self.rng)
            outcome = self.api.process_turn(session, battle_id, attack, defense)
            turns += 1
            turn_result = outcome.get("turnResult") or {}
            if turn_result:
                self.combat.observe(monster_id, turn_result)
            if outcome.get("isOver"):
                finished = True
                break

        self.combat.save()
        time.sleep(self.rng.uniform(1.5, 3.0))
        reward = self.api.finish_activity(session)
        return {
            "battleId": battle_id,
            "monsterId": monster_id,
            "turns": turns,
            "reached_turn_cap": not finished,
            "reward": reward,
        }
