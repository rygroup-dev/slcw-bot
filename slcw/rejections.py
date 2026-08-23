"""Per-wallet memory of calls the server refused.

The server answers "you cannot do that yet" with FAILED_PRECONDITION, and the
engine classifies that as benign — the rejection clears the error counter
instead of tripping it. That is correct for "already done", but it is also what
lets a wallet pick an impossible action forever:

    equipItem  -> FAILED_PRECONDITION "Your grade (1) is too low (Grade 2)"
    openChests -> FAILED_PRECONDITION "Not enough space in inventory"

Both were measured live on 2026-08-21. Ten wallets had been choosing one of them
every cycle for five hours, reporting zero errors and producing nothing, because
the free-value branches sit at the top of `build_candidates` and return early —
so a wallet stuck on one never reaches farming, hunt tasks or the clan branch.

The individual causes are fixed where they belong (a grade gate on gear, a space
gate on chests). This module is the backstop for the ones not yet known: a
refused call is parked for a while and simply not proposed again. The park
expires because a precondition can clear on its own — inventory frees up, grade
rises — so this delays a retry rather than abandoning it.

Only the free-value actions are parkable. An open battle must be resolved before
anything else can be chosen at all, so parking `resumeBattle` would leave the
wallet permanently busy: a worse freeze than the one being fixed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import DATA

MEMORY_PATH = DATA / "rejections.json"

# Long enough that a doomed action stops costing a cycle, short enough that a
# precondition which clears on its own is picked up the same day.
RETRY_AFTER_S = 60 * 60

# Actions safe to skip. Everything absent from this set is always proposed.
PARKABLE = frozenset({
    "openChests",
    "equipItem",
    "upgradeEquip",
    "claimInitialReward",
    "buyLevel",
    "spendAttributePoints",
    "completeNewbieQuest",
    "sellEquipmentItem",
    "deleteInventoryItem",
    "executeBlackMarketOrder",
    "purchaseImperialSeal",
    "payCityEntryFee",
    "evolveGrade",
    "createClan",
    "applyClan",
    "resolveApplication",
    "generateClanQuest",
    "submitQuestResources",
    "makeDonation",
    # A caravan route goes bad in ways that stay bad for a while: the warehouse
    # sold out, the destination filled up, the road was refused.
    "dispatchCaravan",
})


# Some refusals are facts about the game rather than about the wallet that
# heard them. The Black Market's stock is one document per item type with no
# owner, so "shop stock is full" is true for every wallet at once; parking it
# under this shared name saves the other twenty-nine from finding out one
# refused cycle at a time.
FLEET = "*fleet*"


def fingerprint(action: str, params: dict | None) -> str:
    """Stable identity for one call, arguments included.

    Arguments matter: a helmet the wallet cannot wear says nothing about the
    boots it can, so parking has to be per-item rather than per-action.
    """
    return f"{action}:{json.dumps(params or {}, sort_keys=True, default=str)}"


class RejectionMemory:
    """Remembers which calls a wallet should stop trying, and until when."""

    def __init__(self, path: Path = MEMORY_PATH):
        self.path = path
        self.wallets: dict = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(payload, dict):
            self.wallets = payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.wallets, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def park(self, wallet_id: str, action: str, params: dict | None,
             error: str = "", now: float | None = None) -> None:
        if action not in PARKABLE:
            return
        now = time.time() if now is None else now
        entry = self.wallets.setdefault(wallet_id, {})
        entry[fingerprint(action, params)] = {
            "until": now + RETRY_AFTER_S, "error": str(error)[:200]}
        self.save()

    def is_parked(self, wallet_id: str, action: str, params: dict | None,
                  now: float | None = None) -> bool:
        entry = (self.wallets.get(wallet_id) or {}).get(
            fingerprint(action, params))
        if not entry:
            return False
        return (time.time() if now is None else now) < entry.get("until", 0)

    def clear(self, wallet_id: str, action: str, params: dict | None) -> None:
        entry = self.wallets.get(wallet_id) or {}
        if entry.pop(fingerprint(action, params), None) is not None:
            self.save()

    def parked_actions(self, wallet_id: str, now: float | None = None) -> list[str]:
        """What this wallet is currently skipping, for the operator view."""
        now = time.time() if now is None else now
        return sorted(key for key, entry in (self.wallets.get(wallet_id) or {}).items()
                      if now < entry.get("until", 0))
