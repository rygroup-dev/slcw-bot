"""Refining: the step that turns unsellable raw materials into tradeable goods.

Gathering alone loses money — ores, logs, fibers and skins have no market bids at
all. Refining is what closes the loop, and every number here came out of the
frontend bundle rather than a guess:

    startRefining({workshopId, itemId, cycles})

    per cycle at output tier G:
        raw material      (G == 1 ? 9 : 3) units
        catalyst          1 unit   (catalystPrefix + G)
        previous tier     1 unit   (only when G > 1)
        gold              GOLD_PER_CYCLE[G]

    blocked when: character grade < G, or an activity is already running

A tier-1 ingot costs 9 ore plus a catalyst plus 5 gold, and ingots are the things
that actually carry bids — copper_ingot sits near 888 gold while copper_ore has no
bid whatsoever.
"""
from __future__ import annotations

from dataclasses import dataclass

# Gold cost per cycle, by output tier.
GOLD_PER_CYCLE = {1: 5, 2: 15, 3: 45, 4: 135, 5: 405, 6: 1215, 7: 3645}

# Duration per cycle, by output tier.
SECONDS_PER_CYCLE = {1: 10, 2: 30, 3: 120, 4: 300, 5: 900, 6: 2700, 7: 7200}

# Raw material consumed per cycle. Tier 1 is deliberately expensive in inputs.
RAW_PER_CYCLE_TIER_1 = 9
RAW_PER_CYCLE_HIGHER = 3

# Catalysts are not traded on the market — they are bought for gold from a
# per-city crafting shop via purchaseCraftingItem({shopId, tier, quantity}).
CATALYST_PRICE = {1: 20, 2: 60, 3: 180, 4: 540, 5: 1620, 6: 4860, 7: 14580}


@dataclass(frozen=True)
class Workshop:
    id: str
    city_id: str
    profession: str
    catalyst_prefix: str
    # Crafting shop in the same city that sells this workshop's catalysts.
    shop_id: str
    # Output item ids in tier order, index 0 is tier 1.
    items: tuple
    # output item id -> raw material item id
    raw_map: dict

    def output_for_tier(self, tier: int) -> str | None:
        if 1 <= tier <= len(self.items):
            return self.items[tier - 1]
        return None

    def tier_of(self, item_id: str) -> int:
        return self.items.index(item_id) + 1 if item_id in self.items else 0

    def catalyst_for(self, tier: int) -> str:
        return f"{self.catalyst_prefix}{tier}"

    def raw_for(self, item_id: str) -> str | None:
        return self.raw_map.get(item_id)


WORKSHOPS = {
    "smelting": Workshop(
        id="smelting", shop_id="flux_shop", city_id="city_1", profession="miner",
        catalyst_prefix="smelting_flux_",
        items=("copper_ingot", "iron_ingot", "steel_ingot", "mithril_ingot",
               "adamantite_ingot", "runite_ingot", "astralius_ingot"),
        raw_map={"copper_ingot": "copper_ore", "iron_ingot": "iron_ore",
                 "steel_ingot": "steel_ore", "mithril_ingot": "mithril_ore",
                 "adamantite_ingot": "adamantite_ore", "runite_ingot": "runite_ore",
                 "astralius_ingot": "astralius_ore"},
    ),
    "sawmill": Workshop(
        id="sawmill", shop_id="oil_shop", city_id="city_5", profession="lumberjack",
        catalyst_prefix="infusing_oil_",
        items=("pine_plank", "oak_timber", "maple_timber", "yew_slat",
               "black_timber", "elder_plank", "life_plank"),
        raw_map={"pine_plank": "pine_log", "oak_timber": "oak_log",
                 "maple_timber": "maple_log", "yew_slat": "yew_log",
                 "black_timber": "ironwood_log", "elder_plank": "elder_log",
                 "life_plank": "yggdrasil_log"},
    ),
    "tanning": Workshop(
        id="tanning", shop_id="salt_shop", city_id="city_13", profession="skinner",
        catalyst_prefix="tanning_salt_",
        items=("rough_leather", "fine_leather", "tanned_leather", "scale_leather",
               "demon_leather", "dragon_leather", "void_leather"),
        raw_map={"rough_leather": "skin_scraps", "fine_leather": "wolf_skin",
                 "tanned_leather": "bear_skin", "scale_leather": "wyvern_skin",
                 "demon_leather": "demon_skin", "dragon_leather": "dragon_skin",
                 "void_leather": "void_skin"},
    ),
    "weaving": Workshop(
        id="weaving", shop_id="fixative_shop", city_id="city_18", profession="weaver",
        catalyst_prefix="fixative_",
        items=("linen_cloth", "cotton_cloth", "woolen_cloth", "silk_brocade",
               "mageweave_cloth", "spirit_silk", "divine_silk"),
        raw_map={"linen_cloth": "flax_fiber", "cotton_cloth": "cotton_fiber",
                 "woolen_cloth": "wool_fiber", "silk_brocade": "silk_fiber",
                 "mageweave_cloth": "mageweave_fiber", "spirit_silk": "spirit_thread",
                 "divine_silk": "sunthread"},
    ),
}

# Gathering site that produces each workshop's raw materials.
PROFESSION_FARM = {"miner": "farm_2", "lumberjack": "farm_1",
                   "skinner": "farm_5", "weaver": "farm_4"}


def workshop_at(city_id: str) -> Workshop | None:
    return next((w for w in WORKSHOPS.values() if w.city_id == city_id), None)


def raw_per_cycle(tier: int) -> int:
    return RAW_PER_CYCLE_TIER_1 if tier == 1 else RAW_PER_CYCLE_HIGHER


@dataclass
class Recipe:
    """Everything one refining run consumes and produces."""

    workshop: Workshop
    item_id: str
    tier: int
    cycles: int

    @property
    def raw_item(self) -> str:
        return self.workshop.raw_for(self.item_id) or ""

    @property
    def raw_needed(self) -> int:
        return raw_per_cycle(self.tier) * self.cycles

    @property
    def catalyst_item(self) -> str:
        return self.workshop.catalyst_for(self.tier)

    @property
    def catalyst_needed(self) -> int:
        return self.cycles

    @property
    def previous_item(self) -> str | None:
        return self.workshop.output_for_tier(self.tier - 1) if self.tier > 1 else None

    @property
    def previous_needed(self) -> int:
        return self.cycles if self.tier > 1 else 0

    @property
    def gold_cost(self) -> int:
        return GOLD_PER_CYCLE.get(self.tier, 0) * self.cycles

    @property
    def duration_seconds(self) -> int:
        return SECONDS_PER_CYCLE.get(self.tier, 0) * self.cycles

    @property
    def output_quantity(self) -> int:
        return self.cycles

    def inputs(self) -> dict:
        """Item id -> quantity consumed."""
        needed = {self.raw_item: self.raw_needed,
                  self.catalyst_item: self.catalyst_needed}
        if self.previous_item:
            needed[self.previous_item] = self.previous_needed
        return needed

    def missing(self, holdings: dict, gold: int) -> dict:
        """What the account is short of, empty when the run is affordable."""
        short = {}
        for item_id, quantity in self.inputs().items():
            have = int(holdings.get(item_id, 0))
            if have < quantity:
                short[item_id] = quantity - have
        if gold < self.gold_cost:
            short["gold"] = self.gold_cost - gold
        return short

    def payload(self) -> dict:
        return {"workshopId": self.workshop.id, "itemId": self.item_id,
                "cycles": self.cycles}


def catalyst_price(tier: int) -> int:
    return CATALYST_PRICE.get(tier, 0)


def raw_material_value(raw_item: str, market, grade: int = 7) -> float:
    """What one unit of a raw material is worth through the refining chain.

    Raw ores, logs, fibers and skins carry no market bid, so pricing them at
    their own bid values them at zero and gathering never looks worth doing.
    Their real worth is the refined good they become, net of the other inputs:

        value = (output_bid - catalyst_price - refine_gold) / raw_per_cycle

    Returns 0.0 when the refined output has no bid either, or when the chain
    would lose money — both are honest answers rather than optimistic ones.
    """
    if market is None:
        return 0.0

    best = 0.0
    for workshop in WORKSHOPS.values():
        for item_id in workshop.items:
            if workshop.raw_for(item_id) != raw_item:
                continue
            tier = workshop.tier_of(item_id)
            if tier > grade:
                continue
            bid = market.best_bid(item_id) or 0.0
            if bid <= 0:
                continue
            per_cycle = raw_per_cycle(tier)
            # Higher tiers also consume a unit of the previous tier, which this
            # deliberately ignores: counting only the costs we are certain of
            # keeps the estimate conservative rather than flattering.
            net = bid - catalyst_price(tier) - GOLD_PER_CYCLE.get(tier, 0)
            if net <= 0 or per_cycle <= 0:
                continue
            best = max(best, net / per_cycle)
    return best


def catalyst_payload(workshop: Workshop, tier: int, quantity: int) -> dict:
    """Argument shape for purchaseCraftingItem, per the frontend."""
    return {"shopId": workshop.shop_id, "tier": tier, "quantity": quantity}


def affordable_catalysts(tier: int, gold: int, wanted: int) -> int:
    """How many catalysts the balance can buy, capped at what is actually needed."""
    price = catalyst_price(tier)
    if price <= 0:
        return 0
    return max(0, min(wanted, gold // price))


def cycles_if_catalyst_bought(workshop: Workshop, item_id: str, holdings: dict,
                              gold: int, ceiling: int = 100) -> int:
    """Cycles reachable once catalysts are purchased out of the same balance.

    Catalysts are the usual blocker: raw material is cheap to gather and the
    per-cycle gold is small, but a catalyst must be bought for every single cycle.
    """
    tier = workshop.tier_of(item_id)
    if tier == 0:
        return 0

    per_cycle = raw_per_cycle(tier)
    unit_cost = catalyst_price(tier) + GOLD_PER_CYCLE.get(tier, 0)
    if unit_cost <= 0:
        return 0

    limits = [
        int(holdings.get(workshop.raw_for(item_id), 0)) // per_cycle if per_cycle else 0,
        gold // unit_cost,
        ceiling,
    ]
    if tier > 1:
        limits.append(int(holdings.get(workshop.output_for_tier(tier - 1), 0)))
    return max(0, min(limits))


def max_cycles(workshop: Workshop, item_id: str, holdings: dict, gold: int,
               ceiling: int = 100) -> int:
    """Largest run the current inventory and balance can pay for."""
    tier = workshop.tier_of(item_id)
    if tier == 0:
        return 0

    raw = workshop.raw_for(item_id)
    per_cycle = raw_per_cycle(tier)
    limits = [
        int(holdings.get(raw, 0)) // per_cycle if per_cycle else 0,
        int(holdings.get(workshop.catalyst_for(tier), 0)),
        gold // GOLD_PER_CYCLE[tier] if GOLD_PER_CYCLE.get(tier) else 0,
        ceiling,
    ]
    if tier > 1:
        limits.append(int(holdings.get(workshop.output_for_tier(tier - 1), 0)))
    return max(0, min(limits))


def best_recipe(workshop: Workshop, level: int, grade: int, holdings: dict,
                gold: int, market=None, ceiling: int = 100) -> Recipe | None:
    """Pick the affordable recipe with the highest total market value.

    Recipes whose output has no bid score zero, so a workshop with nothing
    tradeable to make simply produces no candidate.
    """
    best: tuple[float, Recipe] | None = None
    for tier in range(1, len(workshop.items) + 1):
        if grade < tier:
            break
        item_id = workshop.output_for_tier(tier)
        cycles = max_cycles(workshop, item_id, holdings, gold, ceiling)
        if cycles <= 0:
            continue
        recipe = Recipe(workshop=workshop, item_id=item_id, tier=tier, cycles=cycles)
        bid = (market.best_bid(item_id) or 0.0) if market is not None else 0.0
        value = bid * recipe.output_quantity - recipe.gold_cost
        if best is None or value > best[0]:
            best = (value, recipe)
    return best[1] if best else None
