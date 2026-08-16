"""Equipment crafting.

    startCrafting({workshopId, recipeId, quantity})

    gold     100 * 3^(tier-1)  per unit
    duration  60 * 2^(tier-1)  seconds per unit
    gates     character grade >= tier
              profession level >= (tier - 1) * 15
              all ingredients held, and no activity running

Unlike refining, crafted equipment does **not** trade — no weapon or armour piece
carries a market bid. Its value is the character it improves, which this engine
cannot measure, so crafting is never chosen automatically. It is exposed as an
operator decision instead: the Telegram view shows exactly what is craftable and
what each run would consume.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .crafting_data import CRAFT_RECIPES, CRAFT_WORKSHOPS

TIER_SUFFIX = re.compile(r"_t(\d)$")

# Profession level required per tier, from the client: (tier - 1) * 15.
PROFESSION_LEVEL_PER_TIER = 15


def tier_of(recipe_id: str) -> int:
    match = TIER_SUFFIX.search(recipe_id)
    return int(match.group(1)) if match else 1


def gold_cost(tier: int, quantity: int = 1) -> int:
    return 100 * (3 ** (tier - 1)) * quantity


def duration_seconds(tier: int, quantity: int = 1) -> int:
    return 60 * (2 ** (tier - 1)) * quantity


def required_profession_level(tier: int) -> int:
    return (tier - 1) * PROFESSION_LEVEL_PER_TIER


@dataclass(frozen=True)
class CraftWorkshop:
    id: str
    city_id: str
    profession: str
    category: str
    items: tuple


WORKSHOPS = {
    wid: CraftWorkshop(id=wid, city_id=entry["city_id"],
                       profession=entry["profession"], category=entry["category"],
                       items=entry["items"])
    for wid, entry in CRAFT_WORKSHOPS.items()
}


def workshops_at(city_id: str) -> list[CraftWorkshop]:
    return [w for w in WORKSHOPS.values() if w.city_id == city_id]


def ingredients_for(recipe_id: str, quantity: int = 1) -> dict:
    return {item: count * quantity
            for item, count in CRAFT_RECIPES.get(recipe_id, ())}


@dataclass
class CraftPlan:
    workshop: CraftWorkshop
    recipe_id: str
    quantity: int = 1

    @property
    def tier(self) -> int:
        return tier_of(self.recipe_id)

    @property
    def gold_cost(self) -> int:
        return gold_cost(self.tier, self.quantity)

    @property
    def duration_seconds(self) -> int:
        return duration_seconds(self.tier, self.quantity)

    def ingredients(self) -> dict:
        return ingredients_for(self.recipe_id, self.quantity)

    def blockers(self, holdings: dict, gold: int, grade: int,
                 professions: dict) -> list[str]:
        """Every reason this run cannot proceed, in the order a player would hit them."""
        problems = []
        if grade < self.tier:
            problems.append(f"grade {grade} below tier {self.tier}")

        needed_level = required_profession_level(self.tier)
        have_level = int((professions or {}).get(self.workshop.profession, 0))
        if have_level < needed_level:
            problems.append(
                f"{self.workshop.profession} level {have_level} below {needed_level}")

        for item, quantity in self.ingredients().items():
            have = int((holdings or {}).get(item, 0))
            if have < quantity:
                problems.append(f"{quantity - have}× {item} short")

        if gold < self.gold_cost:
            problems.append(f"{self.gold_cost - gold} gold short")
        return problems

    def payload(self) -> dict:
        return {"workshopId": self.workshop.id, "recipeId": self.recipe_id,
                "quantity": self.quantity}


def max_quantity(recipe_id: str, holdings: dict, gold: int, ceiling: int = 20) -> int:
    """How many units the current inventory and balance could produce."""
    recipe = CRAFT_RECIPES.get(recipe_id)
    if not recipe:
        return 0
    tier = tier_of(recipe_id)
    limits = [ceiling, gold // gold_cost(tier) if gold_cost(tier) else 0]
    for item, count in recipe:
        limits.append(int((holdings or {}).get(item, 0)) // count if count else 0)
    return max(0, min(limits))


def craftable(city_id: str, holdings: dict, gold: int, grade: int,
              professions: dict) -> list[CraftPlan]:
    """Recipes that could actually be started right now in this city."""
    plans = []
    for workshop in workshops_at(city_id):
        for recipe_id in workshop.items:
            quantity = max_quantity(recipe_id, holdings, gold)
            if quantity <= 0:
                continue
            plan = CraftPlan(workshop=workshop, recipe_id=recipe_id, quantity=quantity)
            if not plan.blockers(holdings, gold, grade, professions):
                plans.append(plan)
    return plans
