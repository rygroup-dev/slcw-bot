"""Per-wallet memory for the newbie quest chain.

The chain has no status endpoint and — measured live on 2026-08-21 — no field in
the player document either. The engine gated it on `doc["newbieQuest"]`, a key
the server never sends, so the counter read 0 on every cycle and the attempt cap
that was supposed to bound it could never engage.

That alone would only waste calls. What made it fatal is the failure mode:

    completeNewbieQuest -> FAILED_PRECONDITION "Insufficient items: 0/1"

FAILED_PRECONDITION is classified benign ("the server already did this"), so the
rejection cleared the error counter instead of tripping it. Every free wallet
therefore picked an action that could not succeed, recorded it as a healthy
cycle, and never reached the battle or farming branches below it — while the
fleet dashboard showed 0 errors on all 25 accounts.

Progress has to be remembered locally because the server exposes none. The item
gate can also clear later, once the wallet actually holds what the step wants, so
a failure parks the chain for a while rather than abandoning it forever.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import DATA

MEMORY_PATH = DATA / "newbie_quests.json"

# How long a wallet waits after a rejected attempt. The chain is item-gated, so
# "not yet" is a real answer that can change once the wallet farms the item —
# but retrying every cycle is what burned four days of fleet time.
RETRY_AFTER_S = 6 * 60 * 60

# Successful steps to allow before the chain is treated as finished. The real
# ceiling is unpublished; steps 6 and 7 were observed paying 400 and 500 XP.
MAX_STEPS = 15


class NewbieQuestMemory:
    """Remembers, per wallet, how far the chain got and when to try it again."""

    def __init__(self, path: Path = MEMORY_PATH):
        self.path = path
        self.wallets: dict = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            self.wallets = payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.wallets, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _entry(self, wallet_id: str) -> dict:
        return self.wallets.setdefault(
            wallet_id, {"steps": 0, "blocked_until": 0, "last_error": ""})

    def is_available(self, wallet_id: str, now: float | None = None) -> bool:
        """True when the chain is worth another call for this wallet."""
        entry = self._entry(wallet_id)
        if entry["steps"] >= MAX_STEPS:
            return False
        return (now or time.time()) >= entry.get("blocked_until", 0)

    def record_success(self, wallet_id: str) -> None:
        entry = self._entry(wallet_id)
        entry["steps"] += 1
        entry["blocked_until"] = 0
        entry["last_error"] = ""
        self.save()

    def record_failure(self, wallet_id: str, message: str,
                       now: float | None = None) -> None:
        """Park the chain for this wallet.

        Called for benign rejections too: "Insufficient items" arrives as
        FAILED_PRECONDITION, and treating that as a no-op is precisely how the
        retry loop stayed invisible.
        """
        entry = self._entry(wallet_id)
        entry["blocked_until"] = (now or time.time()) + RETRY_AFTER_S
        entry["last_error"] = str(message)[:200]
        self.save()
