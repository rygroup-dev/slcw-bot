"""Expected-value scoring.

Every candidate action is converted to a gold-equivalent return and ranked, so the
engine picks what is worth most rather than whatever came first in an if/else ladder.

Energy is priced with a shadow price that rises as energy depletes. When the bar is
full, energy is effectively free and the binding constraint is time, so actions rank
by gold per hour. When the bar is nearly empty, energy dominates and only actions
with a high gold-per-energy ratio survive. The blend is continuous, so there is no
threshold to oscillate around.

Baselines below come from measured live runs, not from documentation:
a city-2 production cycle took 18 minutes, cost 10 energy, and paid 1,000 gold; a
forest spider battle cost 1 energy and about 6 HP and paid 22 XP plus 1-2 spiderfang.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

INFINITE = float("inf")

# Gold-equivalent of one XP point. XP has no market price, so this is a policy dial:
# raise it to favour leveling, lower it to favour raw gold accumulation.
DEFAULT_XP_GOLD = 8.0

# Fallback gold value of one energy point, used before enough data accumulates.
# Production yields 1000 gold for 10 energy, so 100 is the observed floor.
DEFAULT_ENERGY_GOLD = 100.0

# Cost of one HP, expressed through the rest time needed to recover it.
DEFAULT_HP_GOLD = 6.0

PRODUCTION_SECONDS = 18 * 60
PRODUCTION_ENERGY = 10
PRODUCTION_GOLD = 1000

BATTLE_SECONDS = 45
BATTLE_ENERGY = 1
BATTLE_XP = 22
BATTLE_HP_LOSS = 6

RELAX_SECONDS = 110


@dataclass
class ActionScore:
    """A candidate action with its full economic justification."""

    action: str
    params: dict = field(default_factory=dict)
    gold_equivalent: float = 0.0
    energy_cost: int = 0
    gold_cost: int = 0
    hp_cost: int = 0
    duration_seconds: float = 1.0
    score: float = 0.0
    reason: str = ""
    degraded: bool = False

    @property
    def net_gold(self) -> float:
        return self.gold_equivalent - self.gold_cost

    @property
    def gold_per_hour(self) -> float:
        if self.duration_seconds <= 0:
            return INFINITE
        return self.gold_equivalent / (self.duration_seconds / 3600.0)

    @property
    def gold_per_energy(self) -> float:
        if self.energy_cost <= 0:
            return INFINITE
        return self.gold_equivalent / self.energy_cost


@dataclass
class Economy:
    xp_gold: float = DEFAULT_XP_GOLD
    energy_gold: float = DEFAULT_ENERGY_GOLD
    hp_gold: float = DEFAULT_HP_GOLD

    def energy_price(self, energy: int, max_energy: int) -> float:
        """Shadow price of one energy point given how scarce energy currently is."""
        if max_energy <= 0:
            return self.energy_gold
        ratio = max(0.0, min(1.0, energy / max_energy))
        scarcity = (1.0 - ratio) ** 2
        return self.energy_gold * scarcity

    def score_action(self, candidate: ActionScore, energy: int, max_energy: int) -> ActionScore:
        """Fill in `score` as net gold-equivalent per hour after resource costs."""
        if candidate.gold_equivalent == INFINITE:
            candidate.score = INFINITE
            return candidate

        price = self.energy_price(energy, max_energy)
        net = (candidate.gold_equivalent
               - candidate.gold_cost
               - candidate.energy_cost * price
               - candidate.hp_cost * self.hp_gold)
        hours = max(candidate.duration_seconds, 1.0) / 3600.0
        candidate.score = net / hours
        return candidate


def production_candidate(cycles: int = 1) -> ActionScore:
    return ActionScore(
        action="startProduction",
        params={"cycles": cycles},
        gold_equivalent=PRODUCTION_GOLD * cycles,
        energy_cost=PRODUCTION_ENERGY * cycles,
        duration_seconds=PRODUCTION_SECONDS * cycles,
        reason=f"{PRODUCTION_GOLD * cycles} gold over {PRODUCTION_SECONDS * cycles // 60}m",
    )


def battle_candidate(monster_id: str, economy: Economy, drop_values: dict | None = None,
                     expected_drops: dict | None = None, market_stale: bool = False) -> ActionScore:
    """Value a battle as XP plus the market value of its expected drops."""
    drops = expected_drops or {"spiderfang": 1.5}
    values = drop_values or {}
    drop_gold = sum(values.get(item, 0.0) * quantity for item, quantity in drops.items())

    return ActionScore(
        action="battle",
        params={"monsterId": monster_id},
        gold_equivalent=BATTLE_XP * economy.xp_gold + drop_gold,
        energy_cost=BATTLE_ENERGY,
        hp_cost=BATTLE_HP_LOSS,
        duration_seconds=BATTLE_SECONDS,
        reason=(f"{BATTLE_XP} xp @ {economy.xp_gold:g}g + drops worth {drop_gold:.0f}g"
                + (" (market data stale, drops valued at 0)" if market_stale else "")),
        degraded=market_stale,
    )


def farming_candidates(resource, state, market, config) -> list[ActionScore]:
    """Value both funding modes for one gathered resource.

    Raw materials are not traded, so their worth is taken from the refined good
    they become, net of the catalyst and refining gold. When neither the raw
    material nor any refined output it feeds carries a bid, the resource scores
    zero rather than an invented price, and the candidate loses to anything with
    real value instead of quietly winning on optimism.
    """
    from . import farming, refining

    stale = market is None or not market.is_fresh(config.market_ttl_seconds)

    # A raw material is almost never traded directly; what it is worth is the
    # refined good it becomes. Pricing it at its own (absent) bid valued the
    # whole gathering economy at zero.
    bid = (market.best_bid(resource.item_id) or 0.0) if market is not None else 0.0
    via_refining = refining.raw_material_value(
        resource.item_id, market, getattr(state, "grade", 7))
    bid = max(bid, via_refining)

    unpriced = bid <= 0
    tier_note = f"T{resource.tier} {resource.item_id}"
    value_note = " (via refining)" if via_refining > 0 and via_refining >= bid else ""

    candidates = []

    cycles = farming.max_energy_cycles(resource.tier, state.energy, state.gold)
    if cycles > 0:
        cost = farming.energy_mode_cost(resource.tier, cycles)
        candidates.append(ActionScore(
            action="startFarming",
            params=farming.build_payload(resource, "energy", cycles=cycles),
            gold_equivalent=bid * cycles,
            energy_cost=cost["energy"],
            gold_cost=cost["gold"],
            duration_seconds=cycles * 60,
            reason=(f"{tier_note} ×{cycles} energy-mode, "
                    f"{cost['gold']}g + {cost['energy']}en"
                    + (" — no market bid, valued at 0" if unpriced else
                       f", worth {bid:,.0f}g each{value_note}")),
            degraded=stale or unpriced,
        ))

    # Gold mode spends no energy at all, so it can run whenever gold allows.
    for hours in (config.farming_gold_hours,):
        cost = farming.gold_mode_cost(resource.tier, hours)
        if cost["gold"] > state.gold:
            continue
        units = 60 * hours
        candidates.append(ActionScore(
            action="startFarming",
            params=farming.build_payload(resource, "gold", hours=hours),
            gold_equivalent=bid * units,
            energy_cost=0,
            gold_cost=cost["gold"],
            duration_seconds=hours * 3600,
            reason=(f"{tier_note} ×{units} gold-mode {hours}h, {cost['gold']}g, no energy"
                    + (" — no market bid, valued at 0" if unpriced else
                       f", worth {bid:,.0f}g each{value_note}")),
            degraded=stale or unpriced,
        ))

    return candidates


def refining_candidate(recipe, market, config) -> ActionScore:
    """Value a refining run at the market price of what it produces.

    This is the step that makes gathering worth anything: raw materials have no
    bids, refined goods do. A recipe whose output is unpriced scores zero and
    loses to anything measurable, rather than being taken on optimism.
    """
    bid = (market.best_bid(recipe.item_id) or 0.0) if market is not None else 0.0
    stale = market is None or not market.is_fresh(config.market_ttl_seconds)
    unpriced = bid <= 0

    inputs = ", ".join(f"{quantity}× {item}"
                       for item, quantity in recipe.inputs().items())
    return ActionScore(
        action="startRefining",
        params=recipe.payload(),
        gold_equivalent=bid * recipe.output_quantity,
        energy_cost=0,
        gold_cost=recipe.gold_cost,
        duration_seconds=max(1, recipe.duration_seconds),
        reason=(f"{recipe.cycles}× {recipe.item_id} (T{recipe.tier}) from {inputs} "
                f"+ {recipe.gold_cost}g"
                + (" — output has no bid, valued at 0" if unpriced
                   else f", sells {bid:,.0f}g each")),
        degraded=stale or unpriced,
    )


def catalyst_candidate(workshop, tier: int, quantity: int, unlocked_value: float,
                       market_stale: bool) -> ActionScore:
    """Buying catalysts is an investment, valued at the refining run it enables.

    Scoring it by what it unlocks — rather than as a bare gold outflow — is what
    lets the engine spend gold on an input that has no resale value of its own.
    """
    from . import refining

    cost = refining.catalyst_price(tier) * quantity
    return ActionScore(
        action="purchaseCraftingItem",
        params=refining.catalyst_payload(workshop, tier, quantity),
        gold_equivalent=unlocked_value,
        energy_cost=0,
        gold_cost=cost,
        duration_seconds=5.0,
        reason=(f"{quantity}× {workshop.catalyst_for(tier)} for {cost:,}g, "
                f"unlocking ~{unlocked_value:,.0f}g of refining"),
        degraded=market_stale,
    )


def energy_refill_candidate(state) -> ActionScore:
    """A free refill is pure gain, but only while there is room in the bar.

    Calling it at full energy would burn one of the three daily uses for nothing.
    """
    restored = max(0, state.max_energy - state.energy)
    return ActionScore(
        action="refillEnergyFree",
        params={},
        gold_equivalent=INFINITE,
        duration_seconds=1.0,
        score=INFINITE,
        reason=f"free refill restores {restored} energy "
               f"({state.free_refills_left()} left today)",
    )


def relax_candidate(state) -> ActionScore:
    """Resting produces nothing directly; its value is the HP it restores.

    Scoring it explicitly means rest competes with other actions on the same scale
    instead of being hardcoded ahead of them in a ladder.
    """
    missing_hp = max(0, state.max_health - state.health)
    missing_mp = max(0, state.max_mana - state.mana)
    recovered = missing_hp + missing_mp * 0.4
    return ActionScore(
        action="startRelax",
        params={},
        gold_equivalent=recovered * DEFAULT_HP_GOLD,
        energy_cost=0,
        duration_seconds=RELAX_SECONDS,
        reason=f"restores {missing_hp} HP / {missing_mp} MP",
    )


def free_candidate(action: str, params: dict, reason: str) -> ActionScore:
    """Actions that cost nothing and return earned value are always taken first."""
    return ActionScore(
        action=action, params=params, gold_equivalent=INFINITE,
        duration_seconds=1.0, score=INFINITE, reason=reason,
    )


def rank(candidates: list[ActionScore]) -> list[ActionScore]:
    """Highest score first; ties broken by shorter duration."""
    return sorted(candidates, key=lambda c: (-_finite(c.score), c.duration_seconds))


def _finite(value: float) -> float:
    return value if not math.isinf(value) else 10.0 ** 12
