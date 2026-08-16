"""Inventory reading, chest opening, and equipment decisions.

The inventory document is a fixed array of slots:

    {"slotIndex": 1, "templateId": "small_equip_chest", "quantity": 1,
     "instanceId": null}

`instanceId` is null for stackable materials and set for individual equippable
pieces — and `equipItem` takes that instance id, not the template.

Equipping is deliberately conservative. The server rejects a busy slot with
FAILED_PRECONDITION, so the engine only equips where the slot is genuinely free,
and only swaps when the held piece is a strictly higher tier than the worn one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .item_data import ITEM_SLOTS, OPENABLE_ITEMS, SLOT_CONFLICTS

TIER_SUFFIX = re.compile(r"_t(\d)$")


def tier_of(item_id: str) -> int:
    match = TIER_SUFFIX.search(item_id or "")
    return int(match.group(1)) if match else 0


def slot_of(item_id: str) -> str | None:
    return ITEM_SLOTS.get(item_id)


def is_openable(item_id: str) -> bool:
    return item_id in OPENABLE_ITEMS


@dataclass
class Slot:
    index: int
    template_id: str
    quantity: int
    instance_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.template_id or self.quantity <= 0

    @property
    def is_equippable(self) -> bool:
        return self.template_id in ITEM_SLOTS


@dataclass
class Inventory:
    slots: list = field(default_factory=list)
    max_slots: int = 0

    @property
    def used_slots(self) -> int:
        return sum(1 for s in self.slots if not s.is_empty)

    @property
    def free_slots(self) -> int:
        return max(0, self.max_slots - self.used_slots)

    @property
    def is_nearly_full(self) -> bool:
        return self.max_slots > 0 and self.free_slots <= 2

    def holdings(self) -> dict:
        totals: dict = {}
        for slot in self.slots:
            if slot.is_empty:
                continue
            totals[slot.template_id] = totals.get(slot.template_id, 0) + slot.quantity
        return totals

    def chests(self) -> list:
        """Containers waiting to be opened, largest stack first."""
        found = [s for s in self.slots if not s.is_empty and is_openable(s.template_id)]
        return sorted(found, key=lambda s: -s.quantity)

    def equippables(self) -> list:
        """Individual equipment pieces, each carrying its own instance id."""
        return [s for s in self.slots
                if not s.is_empty and s.is_equippable and s.instance_id]


def parse_inventory(document: dict) -> Inventory:
    slots = []
    for raw in (document or {}).get("slots") or []:
        if not isinstance(raw, dict):
            continue
        slots.append(Slot(
            index=int(raw.get("slotIndex", 0) or 0),
            template_id=str(raw.get("templateId") or ""),
            quantity=int(raw.get("quantity", 0) or 0),
            instance_id=raw.get("instanceId") or None,
        ))
    return Inventory(slots=slots, max_slots=int((document or {}).get("maxSlots", 0) or 0))


def worn_tier(equipment: dict, slot: str) -> int:
    """Tier of whatever currently occupies a slot; 0 when empty."""
    worn = (equipment or {}).get(slot)
    if not isinstance(worn, dict):
        return 0
    return tier_of(str(worn.get("templateId") or ""))


def slot_is_free(equipment: dict, slot: str) -> bool:
    """A slot is free only if it and every conflicting slot are unoccupied.

    A two-handed weapon occupies both hands, so equipping one into an apparently
    empty `two_hand_weapon` slot still fails when a one-handed weapon is worn.
    """
    equipment = equipment or {}
    if isinstance(equipment.get(slot), dict) and equipment[slot]:
        return False
    for other in SLOT_CONFLICTS.get(slot, ()):
        if isinstance(equipment.get(other), dict) and equipment[other]:
            return False
    return True


@dataclass
class EquipAction:
    instance_id: str
    template_id: str
    slot: str
    replaces_tier: int = 0

    @property
    def is_upgrade(self) -> bool:
        return self.replaces_tier > 0


def next_equip(inventory: Inventory, equipment: dict) -> EquipAction | None:
    """Best equipment move available, preferring free slots over swaps.

    Filling an empty slot is pure gain and cannot fail. A swap needs the worn
    piece removed first, so it is only proposed when the gain is unambiguous —
    a strictly higher tier in the same slot.
    """
    best: EquipAction | None = None

    for piece in inventory.equippables():
        slot = slot_of(piece.template_id)
        if not slot:
            continue
        held = tier_of(piece.template_id)

        if slot_is_free(equipment, slot):
            action = EquipAction(piece.instance_id, piece.template_id, slot)
        else:
            worn = worn_tier(equipment, slot)
            if held <= worn:
                continue
            action = EquipAction(piece.instance_id, piece.template_id, slot,
                                 replaces_tier=worn)

        # Free slots first, then the biggest tier gain.
        rank = (0 if action.is_upgrade else 1, held - action.replaces_tier)
        if best is None or rank > (0 if best.is_upgrade else 1,
                                   tier_of(best.template_id) - best.replaces_tier):
            best = action
    return best
