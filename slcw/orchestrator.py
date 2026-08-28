"""Decision engine: score every legal action, execute the best one, record why."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace

from . import economy as econ
from . import build as build_mod
from . import farming, inventory as inv_mod, leveling, refining, world
from . import combat as combat_mod
from .combat import CombatMemory, monster_level, select_monster
from . import discard as discard_mod
from . import blackmarket as bm_mod
from . import caravan as caravan_mod
from . import clan as clan_mod
from . import evolution as evo_mod
from .quests import NewbieQuestMemory
from . import rejections as rejections_mod
from .rejections import RejectionMemory
from .config import Config
from .guardrails import GuardrailViolation
from .transport import ApiError

# Expected drops per battle, by monster. Only entries confirmed from live
# rewards appear here; anything else is valued at zero rather than guessed.
EXPECTED_DROPS = {
    "forestspider_lvl1_2": {"spiderfang": 1.5},
}

PRODUCTION_LOCATIONS = {"city_2"}
BATTLE_LOCATIONS = {"farm_3", "wildland_1"}

# Never start a fight below this health ratio, whatever the score says.
BATTLE_MIN_HEALTH_RATIO = 0.45

# Take a free energy refill only once the bar has drained this far, so a daily
# use is never spent restoring a handful of points.
ENERGY_REFILL_RATIO = 0.35

# completeNewbieQuest() takes no arguments and was observed live paying 400,
# then 500 XP with nextQuest incrementing (6, then 7) — a free, escalating
# tutorial chain. There is no status call to say when it ends, so a benign
# rejection past the last step is indistinguishable from "not there yet"
# until it is measured live. This cap bounds the downside: past it, a wallet
# that has actually exhausted the chain would otherwise retry it every cycle
# forever, losing that cycle's battle/farm turn to a call that can never
# succeed. Deliberately conservative pending a live read of the real ceiling.
NEWBIE_QUEST_MAX_ATTEMPTS = 15

# The server accepts a stack, but a smaller batch keeps rewards legible
# in the ledger and bounded if a chest type turns out to be unusual.
MAX_CHESTS_PER_OPEN = 5

# Ceiling on how far ahead a travel decision projects. A wallet arriving to
# fight will repeat until its energy runs out, but a longer horizon is a less
# reliable one, so the estimate is bounded.
MAX_PROJECTED_REPEATS = 40


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

    @property
    def grade(self) -> int:
        # Grade caps which refining tiers a raw material can reach, so it must
        # pass through rather than fall back to a permissive default.
        return self.state.grade


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
    # Carried from the chosen candidate: what the call itself does not say.
    detail: dict = field(default_factory=dict)

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
    economy: econ.Economy | None = None
    combat: CombatMemory = field(default_factory=CombatMemory)
    quests: NewbieQuestMemory = field(default_factory=NewbieQuestMemory)
    rejections: RejectionMemory = field(default_factory=RejectionMemory)
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self):
        # The price of XP is a fleet policy, not a constant, so it comes from
        # the config unless a caller supplied a whole economy of its own.
        if self.economy is None:
            self.economy = econ.Economy(xp_gold=self.config.xp_gold)

    def decide_and_act(self, wallet: dict, session, state, market=None,
                       holdings=None, task_status=None,
                       inventory=None, clan_context=None, cities=None) -> Decision:
        decision = Decision(wallet_id=wallet["id"], dry_run=self.config.dry_run)

        try:
            candidates = self.build_candidates(
                state, market, holdings, task_status=task_status,
                inventory=inventory, wallet_id=wallet["id"],
                clan_context=clan_context, cities=cities)
            decision.considered = candidates
            if not candidates:
                decision.action = "idle"
                decision.reason = self._idle_reason(state)
                return decision

            chosen = candidates[0]
            decision.action = chosen.action
            decision.params = chosen.params
            decision.reason = chosen.reason
            decision.detail = chosen.detail

            if self.config.dry_run:
                decision.result = {"dry_run": True}
                return decision

            decision.result = self.execute(session, chosen, state)
            self.rejections.clear(wallet["id"], chosen.action, chosen.params)
            if chosen.action == "completeNewbieQuest":
                self.quests.record_success(wallet["id"])
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
            # Whether or not it counts as an error, the server said no. Park the
            # exact call so the next cycle picks something else: a free action
            # that is refused every time is how a wallet goes quiet while the
            # dashboard still reads zero errors.
            self.rejections.park(
                wallet["id"], decision.action, decision.params, str(exc))
            # A full shop is a fact about the item type, so park the type as
            # well as the piece — otherwise the next identical piece is offered
            # on the next cycle, and the one after that on the cycle after.
            if (decision.action == "sellEquipmentItem"
                    and "stock" in str(exc).lower()):
                self.rejections.park(
                    rejections_mod.FLEET, "sellEquipmentItem",
                    {"templateId": decision.params.get("templateId", "")}, str(exc))
            # Benign or not, a refused newbie quest is a refusal: the chain is
            # item-gated, and retrying it every cycle is what starved the fleet
            # of real actions while reporting zero errors.
            if decision.action == "completeNewbieQuest":
                self.quests.record_failure(wallet["id"], str(exc))
        return decision

    def _unsellable(self, wallet_id, inventory) -> set:
        """Item types the shop has refused to take, so we stop offering them.

        "Shop stock is full for this item" is about the item type, not the
        piece. Parking the one instance would only put the next identical piece
        forward on the next cycle — which is how a refusal turns into a loop.
        """
        if not wallet_id:
            return set()
        return {piece.template_id for piece in inventory.equippables()
                if self._parked(rejections_mod.FLEET, "sellEquipmentItem",
                                {"templateId": piece.template_id})}

    def _parked(self, wallet_id, action: str, params: dict) -> bool:
        """Whether this exact call was refused recently and should be skipped."""
        if not wallet_id:
            return False
        return self.rejections.is_parked(wallet_id, action, params)

    @staticmethod
    def _quest_items(clan_context) -> tuple:
        """Items the active clan quest wants, which are never destroyed.

        A clan quest is the only sink monster drops have, so its shopping list
        outranks the bag being full.
        """
        quest = (clan_context or {}).get("quest")
        outstanding = quest.outstanding() if quest is not None else {}
        return tuple(outstanding or ())

    def _free(self, wallet_id, action: str, params: dict, reason: str):
        """A free-value candidate, or None when the server just refused it.

        Every free-value branch below returns early, which is what makes them
        free-value: nothing that costs a resource should outrank reward already
        earned. The cost of that shape is that a branch which returns a call the
        server refuses hands the wallet the same doomed action every cycle, at
        zero errors, forever — the failure the rejection memory exists to stop.
        Returning None here lets the caller fall through to the next branch, so
        a parked action costs the wallet nothing instead of costing it the day.
        """
        if self._parked(wallet_id, action, params):
            return None
        return econ.free_candidate(action, params, reason)

    # --- candidate generation -------------------------------------------
    def build_candidates(self, state, market=None, holdings=None,
                         include_travel: bool = True, task_status=None,
                         inventory=None, wallet_id: str | None = None,
                         clan_context=None, cities=None) -> list:
        # Free value first: an expired activity is reward already earned, and
        # unclaimed level rewards cost nothing.
        if state.activity is not None and state.activity.is_settleable:
            # An open battle has to be fought out first: the server rejects
            # finishActivity while the fight is unresolved.
            battle_id = (state.activity.data or {}).get("battleId")
            if state.activity.type == "battle" and battle_id:
                monster_id = (state.activity.data or {}).get("monsterId", "")
                return [econ.free_candidate(
                    "resumeBattle",
                    {"battleId": battle_id, "monsterId": monster_id},
                    f"battle vs {monster_id or 'unknown'} left open, resuming")]
            return [econ.free_candidate(
                "finishActivity", {},
                f"{state.activity.type} finished, reward waiting")]

        if state.is_busy:
            return []

        unclaimed = state.unclaimed_levels()
        if unclaimed:
            # Refused with "Inventory full" while the bag is at 40/40, which is
            # benign — so this has to be skippable or the wallet never gets to
            # the actions that would empty the bag.
            claim = self._free(wallet_id, "claimInitialReward",
                               {"level": unclaimed[0]},
                               f"level {unclaimed[0]} reward unclaimed")
            if claim is not None:
                return [claim]

        # Levelling is manual and free, and each level grants an attribute
        # point — so it comes before spending them.
        if leveling.can_level_up(state.level, state.grade, state.xp):
            level_up = self._free(
                wallet_id, "buyLevel", leveling.payload(),
                f"level {state.level} → {state.level + 1} "
                f"({state.xp:,}/{leveling.xp_required(state.level):,} xp)")
            if level_up is not None:
                return [level_up]

        if state.attribute_points > 0:
            # Points left unspent do nothing at all, and which one to raise is a
            # policy choice rather than a fact — so it is named and configurable.
            target = build_mod.next_attribute(state.attributes, self.config.build)
            spend = self._free(
                wallet_id, "spendAttributePoints",
                {"targetType": "attribute", "targetId": target, "amount": 1},
                f"{state.attribute_points} point(s) unspent → {target} "
                f"({self.config.build} build)")
            if spend is not None:
                return [spend]

        # A tutorial-chain quest that pays pure XP for a no-argument call —
        # free value, so it is taken before anything that costs a resource.
        # `state.newbie_quest` reads a document field the server does not send,
        # so it is always 0 and this cap alone never fires. The local memory is
        # what actually bounds the chain — see slcw/quests.py.
        if (state.newbie_quest < NEWBIE_QUEST_MAX_ATTEMPTS
                and (wallet_id is None or self.quests.is_available(wallet_id))):
            return [econ.free_candidate(
                "completeNewbieQuest", {},
                f"newbie quest chain (step {state.newbie_quest})")]

        # Three free refills a day, and energy gates almost everything. Only worth
        # taking once the bar has drained enough that a refill is not wasted.
        if (state.free_refills_left() > 0
                and state.energy <= state.max_energy * ENERGY_REFILL_RATIO):
            return [econ.energy_refill_candidate(state)]

        # Chests are free loot sitting in a slot, and gear in an empty slot is
        # pure gain. Both cost nothing and neither can be undone badly.
        if inventory is not None:
            # Each chest pays out into a fresh slot, so a batch bigger than the
            # free space is refused with FAILED_PRECONDITION "Not enough space
            # in inventory" — benign, therefore a silent forever-loop.
            chests = inventory.chests()
            batch = min(MAX_CHESTS_PER_OPEN, max(0, inventory.free_slots))
            if chests and batch > 0:
                chest = chests[0]
                params = {"chestTemplateId": chest.template_id,
                          "quantity": min(chest.quantity, batch)}
                if not self._parked(wallet_id, "openChests", params):
                    return [econ.free_candidate(
                        "openChests", params,
                        f"{chest.quantity}× {chest.template_id} unopened")]

            sale = inv_mod.next_sale(
                inventory, state.equipment, state.grade,
                parked=self._unsellable(wallet_id, inventory))
            if sale is not None:
                params = {"instanceId": sale.instance_id,
                          "templateId": sale.template_id}
                if not self._parked(wallet_id, "sellEquipmentItem", params):
                    grade = max(1, state.grade or 1)
                    why = (f"grade {grade} can never wear tier {sale.tier}"
                           if sale.tier > grade else "spare, and the bag is full")
                    return [econ.free_candidate(
                        "sellEquipmentItem", params,
                        f"selling {sale.template_id} back ({why})")]

            equip = inv_mod.next_equip(inventory, state.equipment, state.grade)
            if equip is not None:
                if not equip.is_upgrade:
                    params = {"instanceId": equip.instance_id}
                    if not self._parked(wallet_id, "equipItem", params):
                        return [econ.free_candidate(
                            "equipItem", params,
                            f"{equip.template_id} into empty {equip.slot} slot")]
                # A strictly higher tier in an occupied slot is a pure gain
                # too, but the game needs the worn piece removed first —
                # unequipItem, then equipItem, as one atomic decision.
                # The swap needs a free slot to put the worn piece back into,
                # so a full bag refuses it — benignly, and forever, unless the
                # refusal is allowed to move the wallet on to something else.
                upgrade = self._free(
                    wallet_id, "upgradeEquip",
                    {"slot": equip.slot, "instanceId": equip.instance_id},
                    f"{equip.template_id} upgrades {equip.slot} "
                    f"(tier {equip.replaces_tier} → held)")
                if upgrade is not None:
                    return [upgrade]

            # Last resort, and only ever the last: every route out of a full bag
            # above this line has been tried and refused. Gated on a fresh
            # market because "no bid" is the proof an item is worthless, and a
            # stale book cannot tell that apart from a price it never loaded.
            if self.config.discard_junk:
                fresh = (market is not None
                         and market.is_fresh(self.config.market_ttl_seconds))
                junk = discard_mod.next_discard(
                    inventory, market if fresh else None,
                    quest_items=self._quest_items(clan_context))
                if junk is not None:
                    params = {"slotIndex": junk.slot_index}
                    if not self._parked(wallet_id, "deleteInventoryItem", params):
                        return [econ.free_candidate(
                            "deleteInventoryItem", params,
                            f"destroying {junk.quantity}x {junk.item_id} — "
                            f"no bid, no recipe, no refine, and the bag is "
                            f"{inventory.used_slots}/{inventory.max_slots}",
                            detail={"item_id": junk.item_id,
                                    "quantity": junk.quantity})]

        # A finished hunt task is gold sitting there. This one stays ahead of the
        # clan branch: it is instant, it is free, and the gold it pays is what
        # funds founding a clan in the first place.
        if (task_status is not None and task_status.eligible
                and task_status.can_claim):
            return [econ.free_candidate(
                "claimTaskReward", {},
                f"task reward of {task_status.task.gold_reward:,} gold ready")]

        stale_market = (market is None
                        or not market.is_fresh(self.config.market_ttl_seconds))

        # Refined goods are the one thing the game reliably pays gold for.
        # Pulling every open order showed the player market holds three
        # distinct items and wants premium currency to trade at all, while the
        # Black Market has 6,000 orders whose buy side is entirely refined:
        # copper_ingot at 899 with 6,324 wanted, mithril_ingot at 3,300. This
        # is what makes gathering and refining worth anything.
        if not stale_market:
            resale = bm_mod.next_sale(holdings or {}, market)
            if resale is not None:
                params = {"resourceId": resale.item, "action": "sell",
                          "quantity": resale.quantity}
                if not self._parked(wallet_id, "executeBlackMarketOrder", params):
                    return [econ.free_candidate(
                        "executeBlackMarketOrder", params,
                        f"{resale.quantity}x {resale.item} to the Black Market "
                        f"(~{resale.net:,} gold after tax)")]

        # Raising the grade comes before anything that earns XP, because a
        # wallet at its grade cap earns none: the ceiling is 15 x grade, and
        # thirty wallets sat at level 15 grade 1 throwing away roughly a
        # thousand fights' worth of XP a day. It only fires once the level gate
        # for the next grade is already met, so it never walks a wallet across
        # the map for something it cannot do on arrival.
        ascend = self._evolution_candidate(state, holdings, cities, wallet_id)
        if ascend is not None:
            return [ascend]

        # Clan actions come before the rest of the hunt chain, and the ordering is
        # the whole point. The chain never runs out — finish a task and the next
        # one is waiting — so a wallet in the Borderlands always has a task battle
        # to return, and anything ordered after it is unreachable. Found live on
        # 2026-08-21: wallet-01 sat on 20,314 gold with auto-found armed and kept
        # picking task battles, because `createClan` was never built as a
        # candidate at all. Every clan action shared the same fate.
        #
        # Nothing here competes with the chain for long. Founding happens once
        # ever, applying once per wallet, admitting once per applicant, the quest
        # once a week, and a resource submission empties the stack it submits.
        clan_candidate = self._clan_candidate(
            state, holdings, clan_context, wallet_id)
        if clan_candidate is not None:
            return [clan_candidate]

        # Trading is worked out before the hunt chain because the chain never
        # runs out: a wallet in the Borderlands always has a task battle to
        # return, and the chain returns it as free value, so anything ordered
        # after it is unreachable. That was the right call while fighting was
        # the only gold there was. It is not any more — a load pays 5k-11k for
        # twenty energy where a whole fourteen-kill task pays 2,500 — so a
        # wallet old enough to trade steps out of the chain while it has the
        # energy for a dispatch, and steps back into it when it does not. The
        # task is still waiting: the chain has no clock on it.
        trade = self._caravan_candidate(state, cities, holdings, wallet_id)
        trading_now = trade is not None and state.energy >= caravan_mod.DISPATCH_ENERGY

        if task_status is not None and task_status.eligible and not trading_now:
            # Accepting and fighting are location-gated server-side ("You must be
            # in the Borderlands"), and that refusal classifies as benign — so
            # offering either from a city silently burns the cycle and clears the
            # error counter. Claiming is left ungated: no rejection was observed
            # for it, and a finished task's gold should not wait on travel.
            in_borderlands = state.location_id in BATTLE_LOCATIONS
            if (in_borderlands and task_status.can_fight
                    and state.energy >= econ.BATTLE_ENERGY
                    and state.health_ratio >= BATTLE_MIN_HEALTH_RATIO):
                # The chain is still returned on its own — opening the whole
                # ranking here would let a farming relocation priced at bids
                # the bot cannot collect outbid fighting altogether, which is
                # the bug the comment below records. What has changed is that
                # the task no longer wins by default: the server picks its
                # monster and some of them are ruinous — 127 hit points a kill
                # against a werewolf where a troll costs 28 for the same
                # experience — and at two kills to a rest that is most of the
                # fleet's day spent recovering. So the task kill is priced
                # against the fight the wallet would otherwise pick, and the
                # cheaper one goes.
                return [self._fight_choice(state, market, task_status.task)]
            if in_borderlands and task_status.can_accept:
                return [econ.free_candidate(
                    "acceptTask", {},
                    f"next hunt task available "
                    f"({task_status.completed_count} completed)")]

            # Out of position. The chain is gated to the Borderlands
            # server-side, and it is the only gold this bot has ever actually
            # banked — 1,700 a task, every hour it has run. Everything else the
            # scorer can offer here is priced at market bids the bot cannot
            # collect: nothing it is able to sell is a raw resource.
            #
            # That was harmless while every wallet stood in the Borderlands
            # forever. Raising the grade broke it: a wallet that finishes at
            # the Greyholm altar re-picks from scratch, and on 2026-08-22 the
            # winner was a twenty-minute walk to Crystal Cave, scored at 24,084
            # gold an hour it could never have realised.
            if not in_borderlands and (task_status.can_fight
                                       or task_status.can_accept):
                home = world.nearest(state.location_id, sorted(BATTLE_LOCATIONS))
                if home and home != state.location_id:
                    params = {"destinationId": home}
                    if not self._parked(wallet_id, "startTravel", params):
                        # There may be no task yet — can_accept means the next
                        # one is waiting to be taken, and it is taken there.
                        reward = getattr(task_status.task, "gold_reward", 0) or 0
                        worth = f" ({reward:,}g a task)" if reward else ""
                        return [econ.free_candidate(
                            "startTravel", params,
                            f"back to {world.name_of(home)} "
                            f"for the hunt task{worth}")]

        candidates = []
        stale = market is None or not market.is_fresh(self.config.market_ttl_seconds)

        needs_rest = (state.health_ratio < self.config.rest_hp_ratio
                      or state.mana_ratio < self.config.rest_mp_ratio)
        if needs_rest:
            candidates.append(econ.relax_candidate(state))

        if not self.config.enabled:
            return econ.rank([self._economy_for(state).score_action(c, state.energy, state.max_energy)
                              for c in candidates])

        if state.location_id in PRODUCTION_LOCATIONS and state.energy >= econ.PRODUCTION_ENERGY:
            candidates.append(econ.production_candidate(cycles=1))

        # Refining. This is what makes gathering worth doing at all: raw materials
        # carry no bids, refined goods do.
        workshop = refining.workshop_at(state.location_id)
        if workshop is not None:
            spendable = self.spendable_gold(state, wallet_id)
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
                spendable = self.spendable_gold(state, wallet_id)
                budget_state = _GoldBudget(state, spendable)
                candidates.extend(econ.farming_candidates(
                    resource, budget_state, market, self.config))

        # Monster choice drives both battle and hunting: real combat stats decide
        # what is survivable, and measured outcomes (per-monster, learned from
        # actual fights) decide what is worth fighting. Computed once, unconditional
        # on location, because hunting has no location gate even though battle does.
        if state.energy >= econ.BATTLE_ENERGY:
            stats = build_mod.derive(state.attributes, state.equipment)
            monster = select_monster(
                None, state.level, state.health_ratio,
                weapon_power=stats.weapon_power,
                physical_defense=stats.physical_defense,
                current_health=state.health,
                memory=self.combat, market=market, rng=self.rng)
            if monster:
                # Every monster's own fights teach us its real drop table —
                # prefer that over the small hand-verified EXPECTED_DROPS
                # fallback the moment there is at least one observed battle.
                learned = self.combat.models.get(monster)
                has_learned = bool(learned and learned.battles)
                expected_drops = (learned.avg_drops() if has_learned
                                  else EXPECTED_DROPS.get(monster))

                drop_values = {}
                if not stale:
                    for item in (expected_drops or {}):
                        bid = market.best_bid(item)
                        if bid:
                            drop_values[item] = bid

                if (state.location_id in BATTLE_LOCATIONS
                        and state.health_ratio >= BATTLE_MIN_HEALTH_RATIO):
                    candidates.append(econ.battle_candidate(
                        monster, self._economy_for(state),
                        drop_values=drop_values,
                        expected_drops=expected_drops,
                        market_stale=stale,
                        hp_cost=learned.avg_damage if has_learned else None,
                        xp_estimate=learned.avg_xp if has_learned else None,
                    ))

                # Passive and location-independent, unlike battle — offered
                # everywhere, including gathering zones with no battle option.
                # Valued from this same monster's learned battle data (or the
                # flat fallback) scaled by the measured hunting/battle ratio,
                # rather than a hardcoded single monster.
                xp_estimate = learned.avg_xp if has_learned else econ.BATTLE_XP
                hunt = econ.hunting_candidate(
                    monster, monster_level(monster), self._economy_for(state),
                    xp_estimate, state.energy, state.gold,
                    drop_values=drop_values,
                    expected_drops=expected_drops,
                    market_stale=stale,
                )
                if hunt is not None:
                    candidates.append(hunt)

        # Trading competes with fighting rather than replacing it. A caravan
        # costs twenty energy against a battle's one, so once the bar runs down
        # the scarcity price on energy hands the wallet back to fighting on its
        # own — which is why levelling continues even with XP priced to put
        # gold first.
        if trade is not None:
            candidates.append(trade)

        # Resting is worth considering even at decent health when nothing else is
        # affordable, because it converts idle time into future capacity.
        if not candidates and state.health < state.max_health:
            candidates.append(econ.relax_candidate(state))

        scored = [self._economy_for(state).score_action(c, state.energy, state.max_energy)
                  for c in candidates]

        # Never pay more than an action returns. Rest is exempt: when health is
        # low it is a prerequisite, not a trade.
        profitable = [c for c in scored
                      if c.score > 0 or (c.action == "startRelax" and needs_rest)]

        if include_travel and self.config.auto_travel:
            local_best = max((c.score for c in profitable), default=0.0)
            relocation = self._travel_candidate(
                state, market, holdings, local_best, wallet_id=wallet_id)
            if relocation is not None:
                profitable.append(relocation)

        return econ.rank(profitable)

    def _repeats_at(self, target, state) -> int:
        """How many times the wallet would repeat an action after arriving.

        Energy is the only budget that limits repetition in practice: a battle
        costs one point and can run until the bar empties, while gold-mode
        farming and refining each consume their whole budget in a single call.
        The projection is capped because a longer horizon is a less reliable
        one — prices move and the wallet may be interrupted.
        """
        if target.energy_cost <= 0:
            return 1
        affordable = int(state.energy) // int(target.energy_cost)
        return max(1, min(affordable, MAX_PROJECTED_REPEATS))

    def _economy_for(self, state):
        """The scorer this wallet should be using right now.

        Experience is worth `xp_gold` only while the wallet can still spend it.
        At the grade ceiling every point earned is discarded by the server, so
        the same fight that looked like 330 gold an hour is worth its drops and
        nothing else — and the wallet should be doing whatever actually pays.
        Found live on 2026-08-28: twenty level 15 wallets sat at the grade 1
        ceiling resting and hunting for experience that could not land, while
        the 23,031 gold their ascent needed went unearned.
        """
        if leveling.at_grade_cap(state.level, state.grade):
            return econ.Economy(xp_gold=0.0,
                                energy_gold=self.economy.energy_gold,
                                hp_gold=self.economy.hp_gold)
        return self.economy

    def _fight_choice(self, state, market, task):
        """The task's kill or an ordinary one, whichever is cheaper per hour.

        Both are priced the same way and from the same record: the monster's
        own measured experience, drops and — the part that matters here —
        damage taken. The task carries its completion reward spread over the
        kills it needs, so a task on a cheap monster still wins; one on a
        monster that costs half a health bar a kill does not, and the wallet
        fights something else until the health economics change.

        Deliberately narrow: only these two candidates are compared, because
        everything else in the ranking is priced at market bids this bot has no
        way to collect, and letting those compete here is what once sent a
        wallet on a twenty-minute walk to a farm it could not sell from.
        """
        chain = self._task_battle_candidate(state, market, task)
        alternative = self._plain_battle_candidate(state, market)
        if alternative is None:
            return chain
        rank = [self._economy_for(state).score_action(c, state.energy, state.max_energy)
                for c in (chain, alternative)]
        return max(rank, key=lambda c: c.score)

    def _monster_pricing(self, monster_id: str, market):
        """Everything the scorer needs about one monster: xp, drops, damage."""
        stale = market is None or not market.is_fresh(self.config.market_ttl_seconds)
        learned = self.combat.models.get(monster_id)
        seen = bool(learned and learned.battles)
        drops = learned.avg_drops() if seen else EXPECTED_DROPS.get(monster_id)
        values = {}
        if not stale:
            for item in (drops or {}):
                bid = market.best_bid(item)
                if bid:
                    values[item] = bid
        return {
            "xp": learned.avg_xp if seen else econ.BATTLE_XP,
            "hp_cost": learned.avg_damage if seen else None,
            "expected_drops": drops,
            "drop_values": values,
            "market_stale": stale,
        }

    def _task_battle_candidate(self, state, market, task):
        pricing = self._monster_pricing(task.monster_id, market)
        candidate = econ.task_battle_candidate(
            task.monster_id, self._economy_for(state),
            xp_estimate=pricing.pop("xp"),
            gold_per_kill=task.gold_per_kill,
            **pricing)
        candidate.reason = (f"task battle {task.kills_progress}/{task.kills_required} "
                            f"vs {task.monster_id} for {task.gold_reward:,}g — "
                            + candidate.reason)
        return candidate

    def _plain_battle_candidate(self, state, market):
        """The fight the wallet would pick if there were no task at all."""
        if state.energy < econ.BATTLE_ENERGY:
            return None
        stats = build_mod.derive(state.attributes, state.equipment)
        monster = select_monster(
            None, state.level, state.health_ratio,
            weapon_power=stats.weapon_power,
            physical_defense=stats.physical_defense,
            current_health=state.health,
            memory=self.combat, market=market, rng=self.rng)
        if not monster:
            return None
        pricing = self._monster_pricing(monster, market)
        return econ.battle_candidate(
            monster, self._economy_for(state), xp_estimate=pricing.pop("xp"), **pricing)

    def _caravan_candidate(self, state, cities, holdings=None, wallet_id=None):
        """Buy a load where it is cheap and sell it where it is short.

        Two things gate this and neither is arbitrary. The level gate is the
        operator's: below it a wallet plays exactly as it did before, because
        levelling is still what unlocks grade 3. Standing in a city is the
        game's: `dispatchCaravan` answers "Caravans from cities must go to Hub"
        and the caravan page refuses outright anywhere else, so a wallet in the
        Borderlands has to walk first, and that walk competes on score like
        everything else rather than being forced.

        The load is priced against the live warehouses before the call, so a
        route that has stopped paying is simply not offered.
        """
        if state.level < self.config.caravan_min_level or not cities:
            return None
        if state.energy < caravan_mod.DISPATCH_ENERGY:
            return None
        budget = self.spendable_gold(state, wallet_id)
        if budget <= 0:
            return None

        # Gold is the goal only until the next ascent is paid for. A caravan
        # pays no xp at all, and the first hour of live trading showed exactly
        # what that means: fleet xp went from 6,500 an hour to zero, with every
        # wallet still eight levels short of the grade 3 gate it is saving up
        # for. So a wallet below the level its next grade wants stops trading
        # the moment it can afford the seals, and goes back to fighting for the
        # level. At the cap the reverse holds — xp there is discarded, and
        # trading is the only thing left worth doing.
        step = evo_mod.next_grade(state.grade)
        if step is not None:
            need_level, _ = evo_mod.REQUIREMENTS[step]
            funded = evo_mod.ascent_cost(
                state.grade, (holdings or {}).get(evo_mod.SEAL_ITEM, 0),
                cities.get(evo_mod.CITADEL))
            if state.level < need_level and budget >= funded:
                return None

        here = state.location_id
        if here in cities:
            leg = caravan_mod.best_leg(here, cities, budget)
            if leg is not None:
                params = {"templateId": leg.template_id,
                          "quantity": leg.quantity,
                          "destinationId": leg.destination_id}
                if self._parked(wallet_id, "dispatchCaravan", params):
                    return None
                return econ.ActionScore(
                    action="dispatchCaravan",
                    params=params,
                    # Revenue in, principal out: what survives is the profit,
                    # which is what the score is meant to rank on.
                    gold_equivalent=leg.profit + leg.cost,
                    gold_cost=leg.cost,
                    energy_cost=caravan_mod.DISPATCH_ENERGY,
                    duration_seconds=max(leg.travel_seconds, 1),
                    reason=leg.describe(),
                    detail={"template_id": leg.template_id,
                            "quantity": leg.quantity,
                            "destination_id": leg.destination_id,
                            "cost": leg.cost,
                            "expected_profit": leg.profit},
                )

        if not self.config.auto_travel:
            return None

        # Nowhere to trade from where we stand. Going to the city that would
        # pay the most is worth proposing, but only at its true price: the walk
        # earns nothing by itself, so the profit is spread over the walk and
        # the haul together and ranked against staying put.
        # Judged by what the whole errand returns per second, walk included —
        # the richest warehouse on the map is not worth a twenty-minute hike if
        # a decent one is next door. Then chosen at random from everything
        # within a fifth of the best rate, because thirty wallets taking the
        # same argmax would all queue at the same warehouse and drain it, and
        # the difference between first and fourth place is usually noise.
        options = []
        for city_id, leg in caravan_mod.ranked_origins(cities, budget):
            if city_id == here:
                continue
            walk = world.travel_seconds(here, city_id) / 2
            if walk == float("inf"):
                continue
            errand = max(walk + leg.travel_seconds, 1)
            options.append((leg.profit / errand, city_id, leg, errand))
        if not options:
            return None
        cutoff = max(rate for rate, _, _, _ in options) * 0.8
        _, destination, leg, errand = self.rng.choice(
            [option for option in options if option[0] >= cutoff])

        params = {"destinationId": destination}
        if self._parked(wallet_id, "startTravel", params):
            return None
        return econ.ActionScore(
            action="startTravel",
            params=params,
            gold_equivalent=leg.profit,
            energy_cost=0,
            duration_seconds=errand,
            reason=(f"to {world.name_of(destination)} to trade — "
                    f"{leg.describe()}"),
        )

    def spendable_gold(self, state, wallet_id: str | None = None) -> int:
        """Gold this wallet may commit, after every reserve that applies to it.

        A nominated clan founder spends nothing until it has banked the 20,000
        the call costs. There is no player-to-player gold transfer in this game
        — every candidate endpoint name for one answers 404 — so a clan is paid
        for by a single wallet saving up. A wallet that keeps buying catalysts
        and gathering runs in the meantime never gets there.
        """
        gold = int(state.gold or 0)
        founder = (self.config.clan_founder_wallet or "").strip()
        if founder and wallet_id == founder and gold < clan_mod.CREATE_CLAN_GOLD:
            return 0
        return max(0, gold - self.config.gold_reserve)

    def _evolution_candidate(self, state, holdings, cities=None, wallet_id=None):
        """Travel to Greyholm, buy the seals it is short of, and ascend.

        Seals are spent for good, so the order matters: the level gate is
        checked first, the seals are only bought once standing in the city that
        will accept them, and the ascent is only proposed when they are already
        in hand.
        """
        step = evo_mod.next_grade(state.grade)
        if step is None:
            return None
        need_level, need_seals = evo_mod.REQUIREMENTS[step]
        if state.level < need_level:
            return None
        # Nothing is being wasted until the ceiling is actually reached, and the
        # seals keep indefinitely.
        if not leveling.at_grade_cap(state.level, state.grade):
            return None

        held = int((holdings or {}).get(evo_mod.SEAL_ITEM, 0) or 0)
        short = max(0, need_seals - held)
        # Work out affordability before moving, not after arriving. Greyholm has
        # no farm and no battle zone, so a wallet that travels there without the
        # gold to finish the errand is simply a wallet that has stopped playing.
        # Budgeted at the top of the shop's own price curve rather than today's
        # quote, so the answer cannot go stale in transit.
        if short and self.spendable_gold(state, wallet_id) < evo_mod.ascent_cost(
                state.grade, held, (cities or {}).get(evo_mod.CITADEL), safety=1.25):
            return None

        if state.location_id != evo_mod.CITADEL:
            params = {"destinationId": evo_mod.CITADEL}
            if self._parked(wallet_id, "startTravel", params):
                return None
            return econ.free_candidate(
                "startTravel", params,
                f"to {world.name_of(evo_mod.CITADEL)} for grade {step} "
                f"(level {state.level} is the grade {state.grade} ceiling)")

        if held >= need_seals:
            if self._parked(wallet_id, "evolveGrade", {}):
                return None
            return econ.free_candidate(
                "evolveGrade", {},
                f"grade {state.grade} → {step}, lifting the level cap to "
                f"{leveling.level_cap(step)} ({held} seals)")

        city_number = evo_mod.CITADEL.rsplit("_", 1)[-1]
        if not self._has_city_access(state, city_number):
            params = {"cityId": city_number}
            if not self._parked(wallet_id, "payCityEntryFee", params):
                return econ.free_candidate(
                    "payCityEntryFee", params,
                    f"entry to {world.name_of(evo_mod.CITADEL)} "
                    f"(the shop and the altar are behind it)")
            return None

        batch = min(short, evo_mod.SEAL_BATCH)
        params = {"quantity": batch}
        if self._parked(wallet_id, "purchaseImperialSeal", params):
            return None
        return econ.free_candidate(
            "purchaseImperialSeal", params,
            f"{batch} imperial seal(s) for grade {step} "
            f"(holding {held} of {need_seals})")

    @staticmethod
    def _has_city_access(state, city_number: str) -> bool:
        """Whether this city will let the wallet through its gate right now.

        Citizens are always let in; everyone else holds a timed pass bought
        with payCityEntryFee. Read from the player document rather than guessed
        from a refusal, so no cycle is spent finding out.
        """
        raw = state.raw or {}
        if (raw.get("citizenship") or {}).get(city_number):
            return True
        passes = raw.get("cityAccessPasses") or {}
        try:
            return time.time() < float(passes.get(city_number) or 0)
        except (TypeError, ValueError):
            return False

    def _clan_candidate(self, state, holdings, clan_context, wallet_id=None):
        """Free clan participation, in the order that costs the fleet least.

        Submitting quest resources spends raw drops the market has no bids for,
        so it is pure gain and comes first. Donating gold moves funds into a
        treasury this operator may not control, so it stays behind its own
        switch and never touches the reserve.
        """
        if not self.config.clan_enabled or not clan_context:
            return None

        registry = clan_context.get("registry")
        membership = clan_context.get("membership")
        # The player document is the authority on whether this wallet is in a
        # clan. Deriving it from the membership read alone would let a failed
        # Firestore call look like "no clan" and found a second one.
        in_clan = (bool((state.raw or {}).get("clanId"))
                   or (membership is not None and membership.is_member))

        # Found the clan, once, from the nominated wallet only. Three separate
        # guards have to agree: the operator nominated this wallet, the registry
        # holds no clan id (that is the one that survives a restart), and the
        # server says this wallet is in no clan.
        founder = (self.config.clan_founder_wallet or "").strip()
        if (self.config.clan_auto_found and registry is not None
                and founder and wallet_id == founder
                and not registry.founded and not in_clan
                and self.config.clan_name and self.config.clan_tag
                and state.gold >= clan_mod.CREATE_CLAN_GOLD):
            found = self._free(
                wallet_id, "createClan",
                {"name": self.config.clan_name, "tag": self.config.clan_tag},
                f"founding {self.config.clan_name!r} for "
                f"{clan_mod.CREATE_CLAN_GOLD:,} gold")
            if found is not None:
                return found

        # Any wallet outside the clan applies to it, including one added to the
        # vault long after the clan was founded — nothing here is per-wallet
        # configuration, so a new wallet needs no extra step.
        # The founder is skipped here: it is already the leader, and between
        # createClan returning and its clanId appearing on the player document
        # it would otherwise queue an application to its own clan.
        founded_by_this_wallet = (
            registry is not None
            and str(registry.data.get("founder_wallet") or "") == (wallet_id or ""))
        # A clan of level L holds 5L+5 members, so a new one has ten seats for a
        # fleet of thirty wallets. The runner ranks the wallets outside the clan
        # by level and passes the ones the free seats can actually take; anybody
        # else applying would only park a request in a queue that can never
        # clear it. A runner that passes no ranking keeps the old behaviour.
        seat_holders = clan_context.get("seat_holders")
        holds_a_seat = seat_holders is None or (wallet_id or "") in seat_holders
        if (self.config.clan_auto_join and registry is not None
                and registry.founded and not in_clan and holds_a_seat
                and not founded_by_this_wallet
                and not registry.has_pending_application(wallet_id or "")):
            apply = self._free(
                wallet_id, "applyClan", {"clanId": registry.clan_id},
                f"joining clan {registry.clan_id}")
            if apply is not None:
                return apply

        # Everything below reads this wallet's standing inside the clan. The
        # player document can say "in a clan" while the membership read is still
        # missing or has failed, and that is not enough to act on.
        if not in_clan or membership is None or not membership.is_member:
            return None

        # The leader admits this fleet's own wallets and nobody else. A new clan
        # has ten seats; every stranger admitted costs one of them.
        if membership.role in ("leader", "officer"):
            ours = clan_mod.acceptable_applications(
                clan_context.get("applications") or [],
                membership.clan_id, clan_context.get("fleet_uids") or set(),
                levels=clan_context.get("levels_by_uid"))
            if ours:
                app = ours[0]
                admit = self._free(
                    wallet_id, "resolveApplication",
                    {"applicationId": app.get("applicationId", ""),
                     "action": "accept"},
                    f"admitting {app.get('displayName') or app.get('userId', '?')}")
                if admit is not None:
                    return admit

        quest = clan_context.get("quest")
        # Starting the weekly quest is what buys the clan its seats. A quest
        # pays 3,500 clan XP, and carrying a new clan from level 1 to level 6 —
        # ten seats to thirty-five — costs 3,417, so the fleet outgrows its own
        # founding size on the first quest it finishes. Nothing else measured in
        # the game converts into clan levels at anything like that rate, and the
        # 2,000 raw items it asks for have no market bids to give up.
        if quest is None and membership.role == "leader":
            # The server answers a second request with "cooldown_active", and
            # answers it benignly. Unparked, the leader asked again every cycle
            # for hours — one Telegram message per ask, and no other action all
            # day, because this branch sits above everything that earns.
            generate = self._free(
                wallet_id, "generateClanQuest", {"clanId": membership.clan_id},
                f"starting the weekly clan quest "
                f"(+{clan_mod.QUEST_CLAN_XP:,} clan XP)")
            if generate is not None:
                return generate

        if quest is not None:
            item, amount = clan_mod.submittable(quest, holdings or {})
            if item and amount > 0:
                submit = self._free(
                    wallet_id, "submitQuestResources",
                    {"clanId": membership.clan_id, "questId": quest.quest_id,
                     "itemId": item, "amount": amount},
                    f"{amount}x {item} to clan quest "
                    f"(pool {quest.reward_dkp_pool:,} DKP)")
                if submit is not None:
                    return submit

            # Nothing to hand in, so go and get some. Submitting was built
            # before anything made the fleet collect: a quest asks for 2,000 of
            # one raw drop, raw drops have no market bids, so the monster that
            # supplies them scores zero gold and ordinary monster choice never
            # picks it. The fleet's own quest stood at 471 of 2,000 frogslime
            # after twelve hours — four an hour, every one of them incidental —
            # against a seven-day window it could not have met.
            #
            # This is deliberately a free candidate, ahead of the hunt task
            # chain, and the cost is real: a wallet on the errand stops earning
            # the chain's 1,700 gold a task until the quest is done. It is
            # bounded and it is worth it. One quest pays 3,500 clan XP, which
            # carries a new clan from ten seats to thirty-five, and the fleet
            # has thirty wallets waiting on exactly that.
            errand = self._quest_errand(state, quest)
            if errand is not None:
                return errand

        if self.config.clan_donate_gold and membership.can_donate():
            reserve = max(self.config.gold_reserve, self.config.clan_gold_reserve)
            budget = self.spendable_gold(state, wallet_id)
            amount = clan_mod.affordable_donation(
                min(state.gold, budget + self.config.gold_reserve), reserve)
            if amount >= clan_mod.MIN_GOLD_DONATION:
                donate = self._free(
                    wallet_id, "makeDonation",
                    {"amount": amount, "currency": "gold"},
                    f"daily clan donation {amount:,}g "
                    f"(+{clan_mod.donation_dkp(amount)} DKP)")
                if donate is not None:
                    return donate
        return None

    def _quest_errand(self, state, quest):
        """A battle picked for what it drops rather than what it pays.

        Returns None whenever the fight itself would be a bad idea — no energy,
        too hurt, or standing somewhere combat is refused. That refusal is
        server-side and classifies as benign, so offering a battle from a city
        would burn the cycle and quietly reset the error counter instead of
        showing up as a problem.
        """
        outstanding = quest.outstanding()
        if not outstanding:
            return None
        if (state.energy < econ.BATTLE_ENERGY
                or state.health_ratio < BATTLE_MIN_HEALTH_RATIO
                or state.location_id not in BATTLE_LOCATIONS):
            return None

        # Whichever the quest still needs most, so one errand does not finish a
        # short requirement and leave a long one untouched.
        item = max(outstanding, key=lambda i: outstanding[i])
        monster = combat_mod.best_source(
            item, self.combat, max_level=state.level,
            min_level=state.level - combat_mod.REACH_BELOW)
        if monster is None:
            return None
        return econ.free_candidate(
            "battle", {"monsterId": monster},
            f"{monster} for {outstanding[item]:,} more {item} "
            f"(clan quest, +{quest.reward_clan_xp:,} clan XP)")

    def _travel_candidate(self, state, market, holdings, local_best: float,
                          wallet_id: str | None = None):
        """Consider moving to wherever the next action is worth more.

        Gathering happens at farm zones and refining in city workshops, so a
        wallet that never moves is limited to whatever its current location
        offers.

        A destination is valued by what the wallet would actually do there,
        which is not one action. Arriving somewhere to fight means fighting
        until the energy runs out, so the trip is amortised over the whole stay.
        Charging a 22-minute walk against a single 45-second battle made travel
        look worthless and left every wallet stuck in whatever it happened to
        start doing — gold-rich ones farming forever with no XP, gold-poor ones
        battling forever with no gold.
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
                elsewhere, market, holdings, include_travel=False,
                wallet_id=wallet_id)
            if not remote:
                continue

            target = remote[0]
            repeats = self._repeats_at(target, state)
            stay_seconds = target.duration_seconds * repeats
            total_hours = (seconds + stay_seconds) / 3600.0
            if total_hours <= 0:
                continue
            net = (target.gold_equivalent - target.gold_cost) * repeats
            amortised = net / total_hours

            if best is None or amortised > best[0]:
                best = (amortised, destination, target, seconds, repeats)

        if best is None:
            return None

        amortised, destination, target, seconds, repeats = best
        # Only move when the payoff clearly beats staying put; travel time is
        # dead time and a marginal gain does not justify it.
        if amortised <= local_best * self.config.travel_margin:
            return None

        repeat_note = f" ×{repeats}" if repeats > 1 else ""
        return econ.ActionScore(
            action="startTravel",
            params={"destinationId": destination},
            gold_equivalent=(target.gold_equivalent - target.gold_cost) * repeats,
            energy_cost=0,
            duration_seconds=seconds,
            score=amortised,
            reason=(f"travel {int(seconds) // 60}m to {world.name_of(destination)} "
                    f"for {target.action}{repeat_note} "
                    f"({amortised:,.0f} g/h after travel)"),
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
        if action == "buyLevel":
            return self.api.buy_level(session, candidate.params)
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
        if action == "startHunting":
            p = candidate.params
            return self.api.start_hunting(session, p["monsterId"], p["monsterLevel"],
                                          mode=p["mode"], cycles=p["cycles"],
                                          hours=p["hours"])
        if action == "startRefining":
            return self.api.start_refining(session, candidate.params)
        if action == "refillEnergyFree":
            return self.api.refill_energy_free(session)
        if action == "purchaseCraftingItem":
            return self.api.purchase_crafting_item(session, candidate.params)
        if action == "startTravel":
            return self.api.start_travel(session, candidate.params["destinationId"])
        if action == "dispatchCaravan":
            p = candidate.params
            result = self.api.dispatch_caravan(
                session, p["templateId"], p["quantity"], p["destinationId"])
            # The load is paid for now and sold on arrival, through
            # finishActivity. Recording only the arrival would credit the
            # wallet the whole sale and never the gold it was bought with, so
            # the outlay rides along in the result for the ledger to subtract.
            if isinstance(result, dict):
                result.setdefault("goldSpent", int(candidate.gold_cost))
            return result
        if action == "completeNewbieQuest":
            return self.api.complete_newbie_quest(session)
        if action == "acceptTask":
            return self.api.accept_task(session)
        if action == "claimTaskReward":
            return self.api.claim_task_reward(session)
        if action == "openChests":
            return self.api.open_chests(
                session, candidate.params["chestTemplateId"],
                candidate.params["quantity"])
        if action == "deleteInventoryItem":
            return self.api.delete_inventory_item(
                session, candidate.params["slotIndex"])
        if action == "equipItem":
            return self.api.equip_item(session, candidate.params["instanceId"])
        if action == "upgradeEquip":
            unequip_result = self.api.unequip_item(session, candidate.params["slot"])
            equip_result = self.api.equip_item(session, candidate.params["instanceId"])
            return {"unequip": unequip_result, "equip": equip_result}
        if action == "createClan":
            return self.api.create_clan(
                session, candidate.params["name"], candidate.params["tag"])
        if action == "applyClan":
            return self.api.apply_clan(session, candidate.params["clanId"])
        if action == "resolveApplication":
            return self.api.resolve_clan_application(
                session, candidate.params["applicationId"],
                candidate.params["action"])
        if action == "generateClanQuest":
            return self.api.generate_clan_quest(session, candidate.params["clanId"])
        if action == "submitQuestResources":
            p = candidate.params
            return self.api.submit_quest_resources(
                session, p["clanId"], p["questId"], p["itemId"], p["amount"])
        if action == "makeDonation":
            return self.api.make_donation(
                session, candidate.params["amount"], candidate.params["currency"])
        if action == "resumeBattle":
            return self.resume_battle(session, candidate.params["battleId"],
                                      candidate.params["monsterId"])
        if action == "executeBlackMarketOrder":
            p = candidate.params
            return self.api.execute_black_market_order(
                session, p["resourceId"], p["action"], p["quantity"])
        if action == "payCityEntryFee":
            return self.api.pay_city_entry_fee(session, candidate.params["cityId"])
        if action == "purchaseImperialSeal":
            return self.api.purchase_imperial_seal(
                session, candidate.params.get("quantity", 1))
        if action == "evolveGrade":
            return self.api.evolve_grade(session)
        if action == "sellEquipmentItem":
            return self.api.sell_equipment_item(session, candidate.params["instanceId"])
        if action == "battle":
            return self.run_battle(session, candidate.params["monsterId"])
        if action == "startTaskBattle":
            return self.run_task_battle(session, candidate.params["monsterId"])
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
        return self._fight_and_settle(session, battle_id, monster_id)

    def _fight_and_settle(self, session, battle_id: str, monster_id: str) -> dict:
        """Drive an already-open battle to its end, then settle it.

        Split out of run_battle so a battle found open at startup can be
        finished by exactly the same path that started it — the server refuses
        finishActivity (HTTP 500) while a fight is still unresolved, so
        resuming has to fight, not just settle.
        """
        turns = 0
        damage_taken = 0
        finished = False
        for _ in range(self.config.battle_max_turns):
            time.sleep(self.rng.uniform(1.8, 3.6))
            attack, defense = self.combat.choose_zones(monster_id, self.rng)
            try:
                outcome = self.api.process_turn(session, battle_id, attack, defense)
            except ApiError as exc:
                # "Battle is not active": the server has already decided this
                # fight and is only waiting for the activity to be closed. That
                # status is benign, so letting it propagate skipped the settle
                # below — which left one wallet holding a won battle, and
                # therefore reading as busy, for nine and a half hours.
                if not exc.is_benign:
                    raise
                finished = True
                break
            turns += 1
            turn_result = outcome.get("turnResult") or {}
            if turn_result:
                self.combat.observe(monster_id, turn_result)
                incoming = turn_result.get("monster") or {}
                if incoming.get("type") in ("hit", "crit"):
                    damage_taken += int(incoming.get("damage", 0) or 0)
            if outcome.get("isOver"):
                finished = True
                break

        time.sleep(self.rng.uniform(1.5, 3.0))
        reward = self.api.finish_activity(session)

        # Record what the fight actually cost and returned. Drop tables are
        # server-side, so this is the only way the engine can learn a monster's
        # worth rather than assume it.
        summary = (reward or {}).get("rewardSummary") or {}
        self.combat.record_battle(monster_id, summary, turns, damage_taken)
        self.combat.save()
        return {
            "battleId": battle_id,
            "monsterId": monster_id,
            "turns": turns,
            "reached_turn_cap": not finished,
            "reward": reward,
        }

    def resume_battle(self, session, battle_id: str, monster_id: str) -> dict:
        """Finish a battle that was left open by an earlier run.

        A battle activity carries no endTime, so a process that died mid-fight
        left the wallet holding one forever: never expired, therefore always
        "busy", therefore never acted on again.
        """
        return self._fight_and_settle(session, battle_id, monster_id)

    def run_task_battle(self, session, monster_id: str) -> dict:
        """Fight the hunt task's assigned monster to a conclusion.

        startTaskBattle takes no arguments — the server already knows which
        monster the active task points at — but everything after that is the
        same processTurn/finishActivity cycle as a normal battle, so it shares
        the one implementation rather than keeping a second copy of it.
        """
        started = self.api.start_task_battle(session)
        battle_id = started.get("battleId")
        if not battle_id:
            raise ApiError("startTaskBattle returned no battleId")
        return self._fight_and_settle(session, battle_id, monster_id)
