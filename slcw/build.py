"""Character stats and attribute allocation.

The derivation below is taken from the profile page, not guessed:

    weaponPower      = 2·might        + equipment.weaponPower
    spellPower       = 2·intelligence + equipment.spellPower
    physicalDefense  = 2·vitality + 1·might + 1.5·dexterity + equipment.physicalDefense
    magicalDefense   = 1·vitality + 1.5·dexterity + 2·wisdom + equipment.magicalDefense
    precision        = 2·dexterity
    impact           = 2·dexterity
    maxHP            = 100 + 10·vitality
    maxMana          = 100 + 10·wisdom

Wearing five or more pieces of one armour class at the same tier grants a set
bonus: plate multiplies physical defence by 1.2, cloth multiplies magical
defence and spell power by 1.2.

Allocation exists because points arrive every level and, left unspent, do
nothing at all. Which build to pursue is a policy choice rather than a fact, so
it is named, explained, and switchable — the engine should not quietly decide
how the character grows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ATTRIBUTES = ("vitality", "might", "dexterity", "wisdom", "intelligence")

BASE_HEALTH = 100
BASE_MANA = 100
PER_POINT_HEALTH = 10
PER_POINT_MANA = 10

SET_BONUS_PIECES = 5
SET_BONUS_MULTIPLIER = 1.2


@dataclass
class DerivedStats:
    weapon_power: float = 0.0
    spell_power: float = 0.0
    physical_defense: float = 0.0
    magical_defense: float = 0.0
    precision: float = 0.0
    impact: float = 0.0
    max_health: int = BASE_HEALTH
    max_mana: int = BASE_MANA
    set_bonus: str = ""


def equipment_bonuses(equipment: dict) -> tuple[dict, dict]:
    """Split worn gear into attribute bonuses and flat stat bonuses."""
    attrs = {name: 0.0 for name in ATTRIBUTES}
    flats = {"weaponPower": 0.0, "spellPower": 0.0,
             "physicalDefense": 0.0, "magicalDefense": 0.0}

    for piece in (equipment or {}).values():
        if not isinstance(piece, dict) or not piece:
            continue

        bonus = piece.get("bonusStats")
        if isinstance(bonus, list):
            for entry in bonus:
                name = (entry or {}).get("attribute")
                if name in attrs:
                    attrs[name] += float(entry.get("value", 0) or 0)
        elif isinstance(bonus, dict):
            for name, value in bonus.items():
                if name in attrs:
                    attrs[name] += float(value or 0)

        stats = piece.get("stats")
        if isinstance(stats, dict):
            stat_type = stats.get("statType")
            base = stats.get("baseValue")
            if stat_type in flats and isinstance(base, (int, float)):
                flats[stat_type] += float(base)
    return attrs, flats


def armour_set(equipment: dict) -> tuple[str, int] | None:
    """The armour class and tier worn on at least five pieces, if any.

    Item ids look like `plate_helmet_t3`; `magic` counts as cloth, which is how
    the client groups it.
    """
    import re

    counts: dict = {}
    pattern = re.compile(r"^(plate|leather|cloth|magic)_[a-z]+_t(\d+)$")
    for piece in (equipment or {}).values():
        template = (piece or {}).get("templateId") if isinstance(piece, dict) else None
        if not template:
            continue
        match = pattern.match(template)
        if not match:
            continue
        kind = "cloth" if match.group(1) == "magic" else match.group(1)
        key = (kind, int(match.group(2)))
        counts[key] = counts.get(key, 0) + 1

    for (kind, tier), count in counts.items():
        if count >= SET_BONUS_PIECES:
            return kind, tier
    return None


def derive(attributes: dict, equipment: dict | None = None) -> DerivedStats:
    """Combat stats a character actually fights with."""
    base = {name: float((attributes or {}).get(name, 3) or 0) for name in ATTRIBUTES}
    attr_bonus, flat = equipment_bonuses(equipment or {})
    total = {name: base[name] + attr_bonus[name] for name in ATTRIBUTES}

    stats = DerivedStats(
        weapon_power=2 * total["might"] + flat["weaponPower"],
        spell_power=2 * total["intelligence"] + flat["spellPower"],
        physical_defense=(2 * total["vitality"] + total["might"]
                          + 1.5 * total["dexterity"] + flat["physicalDefense"]),
        magical_defense=(total["vitality"] + 1.5 * total["dexterity"]
                         + 2 * total["wisdom"] + flat["magicalDefense"]),
        precision=2 * total["dexterity"],
        impact=2 * total["dexterity"],
        # Health and mana follow the raw attribute, not the equipment-boosted one.
        max_health=int(BASE_HEALTH + PER_POINT_HEALTH * base["vitality"]),
        max_mana=int(BASE_MANA + PER_POINT_MANA * base["wisdom"]),
    )

    worn = armour_set(equipment or {})
    if worn:
        kind, tier = worn
        stats.set_bonus = f"{kind} t{tier}"
        if kind == "plate":
            stats.physical_defense *= SET_BONUS_MULTIPLIER
        elif kind == "cloth":
            stats.magical_defense *= SET_BONUS_MULTIPLIER
            stats.spell_power *= SET_BONUS_MULTIPLIER
    return stats


# --- allocation policy ---------------------------------------------------

@dataclass(frozen=True)
class Build:
    """A named allocation policy, with the reasoning attached."""

    name: str
    weights: dict
    summary: str

    def next_attribute(self, attributes: dict) -> str:
        """Which attribute is furthest behind its target share.

        Weighted round-robin rather than dumping everything into one stat: the
        target ratio is held at every point spent, so the build stays balanced
        even if points arrive in bursts.
        """
        current = {name: float((attributes or {}).get(name, 0) or 0)
                   for name in self.weights}
        total_now = sum(current.values()) or 1.0
        total_weight = sum(self.weights.values()) or 1.0

        # Pick whichever attribute has the largest shortfall against its share.
        def shortfall(name: str) -> float:
            target = self.weights[name] / total_weight
            actual = current[name] / total_now
            return target - actual

        return max(self.weights, key=lambda name: (shortfall(name), self.weights[name]))


# Physical monsters are what the fleet fights, so magical defence and spell
# power earn nothing here; intelligence and wisdom are deliberately absent.
BUILDS = {
    "sustain": Build(
        name="sustain",
        weights={"vitality": 3, "dexterity": 2, "might": 1},
        summary=("Vitality leads because it is the only attribute that raises max "
                 "health, and it doubles into physical defence — every point is "
                 "fewer rest cycles, and resting is dead time. Dexterity is next "
                 "for feeding defence, precision and impact at once. Might is kept "
                 "small: killing faster helps, but a dead character farms nothing."),
    ),
    "balanced": Build(
        name="balanced",
        weights={"vitality": 2, "dexterity": 2, "might": 2},
        summary="Equal split across health, accuracy and damage.",
    ),
    "damage": Build(
        name="damage",
        weights={"might": 3, "dexterity": 2, "vitality": 1},
        summary=("Might first for weapon power, so fights end in fewer turns. "
                 "Takes more damage and rests more often."),
    ),
}

DEFAULT_BUILD = "sustain"


def get_build(name: str) -> Build:
    return BUILDS.get((name or "").lower(), BUILDS[DEFAULT_BUILD])


def next_attribute(attributes: dict, build_name: str = DEFAULT_BUILD) -> str:
    return get_build(build_name).next_attribute(attributes)
