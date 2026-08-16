"""Gathering (farming) catalog and cost model.

Reverse-engineered from the frontend bundle. The call is:

    startFarming({itemId, mode, cycles, hours, profession, tier})

with three funding modes whose costs the client computes locally before sending:

    energy mode: gold = 3^(tier-1) * cycles,  energy = cycles,  1 cycle = 1 minute
    gold mode:   gold = round(hours/8*1500) + 60*hours*3^(tier-1),  energy = 0
    diamond mode: diamonds = 49 * 3^(tier-1)   <- denied by guardrails

Gold mode is the interesting one: it converts gold into resources over 1-8 hours
without spending any energy, so it runs in parallel with the energy economy rather
than competing with it.
"""
from __future__ import annotations

from dataclasses import dataclass

# locationId -> profession and its resource ladder, verbatim from the bundle.
FARM_LOCATIONS = {
    "farm_1": {
        "profession": "lumberjack",
        "resources": [
            {"itemId": "pine_log", "tier": 1, "requiredLevel": 0},
            {"itemId": "oak_log", "tier": 2, "requiredLevel": 15},
            {"itemId": "maple_log", "tier": 3, "requiredLevel": 30},
            {"itemId": "yew_log", "tier": 4, "requiredLevel": 45},
            {"itemId": "ironwood_log", "tier": 5, "requiredLevel": 60},
            {"itemId": "elder_log", "tier": 6, "requiredLevel": 75},
            {"itemId": "yggdrasil_log", "tier": 7, "requiredLevel": 90},
        ],
    },
    "farm_2": {
        "profession": "miner",
        "resources": [
            {"itemId": "copper_ore", "tier": 1, "requiredLevel": 0},
            {"itemId": "iron_ore", "tier": 2, "requiredLevel": 15},
            {"itemId": "steel_ore", "tier": 3, "requiredLevel": 30},
            {"itemId": "mithril_ore", "tier": 4, "requiredLevel": 45},
            {"itemId": "adamantite_ore", "tier": 5, "requiredLevel": 60},
            {"itemId": "runite_ore", "tier": 6, "requiredLevel": 75},
            {"itemId": "astralius_ore", "tier": 7, "requiredLevel": 90},
        ],
    },
    "farm_4": {
        "profession": "weaver",
        "resources": [
            {"itemId": "flax_fiber", "tier": 1, "requiredLevel": 0},
            {"itemId": "cotton_fiber", "tier": 2, "requiredLevel": 15},
            {"itemId": "wool_fiber", "tier": 3, "requiredLevel": 30},
            {"itemId": "silk_fiber", "tier": 4, "requiredLevel": 45},
            {"itemId": "mageweave_fiber", "tier": 5, "requiredLevel": 60},
            {"itemId": "spirit_thread", "tier": 6, "requiredLevel": 75},
            {"itemId": "sunthread", "tier": 7, "requiredLevel": 90},
        ],
    },
    "farm_5": {
        "profession": "skinner",
        "resources": [
            {"itemId": "skin_scraps", "tier": 1, "requiredLevel": 0},
            {"itemId": "wolf_skin", "tier": 2, "requiredLevel": 15},
            {"itemId": "bear_skin", "tier": 3, "requiredLevel": 30},
            {"itemId": "wyvern_skin", "tier": 4, "requiredLevel": 45},
            {"itemId": "demon_skin", "tier": 5, "requiredLevel": 60},
            {"itemId": "dragon_skin", "tier": 6, "requiredLevel": 75},
            {"itemId": "void_skin", "tier": 7, "requiredLevel": 90},
        ],
    },
}

MAX_ENERGY_CYCLES = 100
MAX_GOLD_HOURS = 8
GOLD_MODE_BASE = 1500


def tier_multiplier(tier: int) -> int:
    return 3 ** (tier - 1)


def energy_mode_cost(tier: int, cycles: int) -> dict:
    return {"gold": tier_multiplier(tier) * cycles, "energy": cycles, "diamonds": 0}


def gold_mode_cost(tier: int, hours: int) -> dict:
    gold = round(hours / MAX_GOLD_HOURS * GOLD_MODE_BASE) + 60 * hours * tier_multiplier(tier)
    return {"gold": gold, "energy": 0, "diamonds": 0}


def max_energy_cycles(tier: int, energy: int, gold: int) -> int:
    """Client-side cap: min(100, energy, gold // 3^(tier-1))."""
    affordable = gold // tier_multiplier(tier) if tier_multiplier(tier) else gold
    return max(0, min(MAX_ENERGY_CYCLES, energy, affordable))


@dataclass
class Resource:
    item_id: str
    tier: int
    required_level: int
    profession: str
    location_id: str


def resources_at(location_id: str) -> list[Resource]:
    entry = FARM_LOCATIONS.get(location_id)
    if not entry:
        return []
    return [Resource(item_id=r["itemId"], tier=r["tier"],
                     required_level=r["requiredLevel"],
                     profession=entry["profession"], location_id=location_id)
            for r in entry["resources"]]


def eligible_resources(location_id: str, level: int, grade: int) -> list[Resource]:
    """Resources the character may actually gather.

    The client blocks the button when `grade < resource.tier`, and each resource
    additionally carries a level requirement.
    """
    return [r for r in resources_at(location_id)
            if grade >= r.tier and level >= r.required_level]


def best_resource(location_id: str, level: int, grade: int,
                  market=None) -> Resource | None:
    """Pick the eligible resource with the highest market bid, else the highest tier.

    Falling back to tier keeps the choice sensible before any market data exists,
    since higher tiers cost more precisely because they are worth more.
    """
    eligible = eligible_resources(location_id, level, grade)
    if not eligible:
        return None
    if market is not None:
        priced = [(market.best_bid(r.item_id) or 0.0, r) for r in eligible]
        best_price, best = max(priced, key=lambda pair: (pair[0], pair[1].tier))
        if best_price > 0:
            return best
    return max(eligible, key=lambda r: r.tier)


def build_payload(resource: Resource, mode: str, cycles: int = 0, hours: int = 0) -> dict:
    """Exact argument shape the frontend sends to startFarming."""
    return {
        "itemId": resource.item_id,
        "mode": mode,
        "cycles": 60 * hours if mode == "gold" else cycles,
        "hours": hours if mode == "gold" else 0,
        "profession": resource.profession,
        "tier": resource.tier,
    }
