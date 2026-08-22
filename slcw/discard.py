"""Destroying the drops nothing in the game can use.

This is the only irreversible thing the bot does to an item, so the rule is
inverted from everywhere else: an item is kept unless it can be *proved*
worthless, and the proof has to fail closed.

The reason it exists at all was measured on 2026-08-22. Twenty-four of thirty
wallets sat at 40/40 slots, and the bag could not be emptied by any route the
game offers:

    market bid on a monster drop      none, on any of them
    crafting recipe using one         none, across all 165 recipe inputs
    refining input                    none, across all 28 raw materials
    sellEquipmentItem                 "Shop stock is full", every gear template
    expandInventory                   costs diamonds; every wallet holds zero

A full bag then refuses openChests, claimInitialReward and upgradeEquip, so the
fighting that fills it also stops paying out. Monster drops have exactly one
sink in the whole game — a clan quest asking for 2,000 of one of them — and a
quest names one item, while a wallet accumulates thirty-one kinds.

Five gates, all of which must pass before a stack is even a candidate:

    1. it is not equipment          gear sells for real gold once shop stock
                                    rotates, so it is never destroyed
    2. it is not a container        a chest is unopened loot, not clutter
    3. it is not a game currency    imperial seals buy grades
    4. nothing consumes it          not a crafting input, not a refining input,
                                    not a refined good
    5. nothing bids on it           and the market must be FRESH to say so — a
                                    stale book cannot tell "worth nothing" from
                                    "price not loaded", and guessing there
                                    destroys something valuable

Then the active clan quest's items are removed, because that is the one sink
that does exist. What is left is ordered smallest stack first: freeing a slot
should cost the fewest items, and it leaves the large stacks — the ones a quest
could actually finish — standing.
"""
from __future__ import annotations

from dataclasses import dataclass

import json
import os
import time

from .blackmarket import REFINED_ITEMS
from .crafting_data import CRAFT_RECIPES
from .item_data import ITEM_SLOTS, OPENABLE_ITEMS
from .config import DATA
from .refining import WORKSHOPS

# Deliberately not the profit ledger: that file answers "what did the fleet
# earn", and every row in it is summed into gold-per-hour. A deletion is a loss
# with no reward shape at all, and the reason it needs writing down is the
# opposite one — it is the only thing here that cannot be undone or read back
# out of the game afterwards.
LOG_PATH = DATA / "discards.jsonl"

# Everything any crafting recipe consumes, at any tier.
CRAFTING_INPUTS = frozenset(
    material for recipe in CRAFT_RECIPES.values() for material, _ in recipe)

# Everything refining consumes to make the goods the market bids on.
REFINING_INPUTS = frozenset(
    raw for workshop in WORKSHOPS.values()
    for raw in (workshop.raw_map or {}).values())

# Named rather than pattern-matched: a currency that buys grades is not clutter
# however little the market says it is worth.
CURRENCIES = frozenset({"imperial_seal"})

# Catalysts are bought for gold and consumed by refining, and their ids are not
# in any of the sets above — they come from a per-workshop prefix.
CATALYSTS = frozenset(
    f"{workshop.catalyst_prefix}{tier}"
    for workshop in WORKSHOPS.values() for tier in range(1, 8))

PROTECTED = (frozenset(ITEM_SLOTS) | frozenset(OPENABLE_ITEMS)
             | REFINED_ITEMS | CRAFTING_INPUTS | REFINING_INPUTS
             | CURRENCIES | CATALYSTS)


def is_worthless(item_id: str, market) -> bool:
    """True only when every use the game has for this item has been ruled out.

    `market` must already be known fresh. Passing a stale book here would let a
    missing quote read as "no bid", which is the one mistake this module cannot
    take back.
    """
    if not item_id or item_id in PROTECTED:
        return False
    if market is None:
        return False
    return not market.best_bid(item_id)


@dataclass(frozen=True)
class Discard:
    slot_index: int
    item_id: str
    quantity: int


def next_discard(inventory, market, quest_items=(), keep_kinds: int = 0):
    """The one stack to destroy, or None to destroy nothing.

    Only fires on a bag with no free slot left: until then the drops cost
    nothing to hold, and holding them is how a clan quest gets fed.
    """
    if inventory is None or market is None:
        return None
    if inventory.max_slots <= 0 or inventory.free_slots > 0:
        return None

    protected_by_quest = {str(item) for item in (quest_items or ())}
    candidates = [
        slot for slot in inventory.slots
        if not slot.is_empty
        and not slot.instance_id          # an instance id means a real piece of gear
        and slot.template_id not in protected_by_quest
        and is_worthless(slot.template_id, market)]
    if not candidates:
        return None

    # Smallest stack first: the fewest items destroyed per slot recovered, and
    # the stacks nearest a clan quest's 2,000 are the last to go.
    candidates.sort(key=lambda slot: (slot.quantity, slot.template_id))
    if keep_kinds and len(candidates) <= keep_kinds:
        return None
    chosen = candidates[0]
    return Discard(chosen.index, chosen.template_id, chosen.quantity)


def record(wallet_id: str, item_id: str, quantity: int) -> dict:
    """Append one destroyed stack to the audit log."""
    row = {"ts": int(time.time()), "wallet_id": wallet_id,
           "item_id": item_id, "quantity": int(quantity)}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.chmod(LOG_PATH, 0o600)
    return row


def totals() -> dict:
    """Everything destroyed so far, by item. For the operator view."""
    counts: dict = {}
    if not LOG_PATH.exists():
        return counts
    with LOG_PATH.open() as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = row.get("item_id")
            if item:
                counts[item] = counts.get(item, 0) + int(row.get("quantity", 0))
    return counts
