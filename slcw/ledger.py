"""Realized reward accounting."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .config import DATA

LEDGER_PATH = DATA / "profit_ledger.jsonl"


def record(wallet_id: str, action: str, result: dict) -> dict | None:
    """Append a realized reward. Returns the row written, or None if nothing landed."""
    summary = _extract_summary(action, result)
    if not summary:
        return None
    row = {
        "ts": int(time.time()),
        "wallet_id": wallet_id,
        "action": action,
        "reward": summary,
    }
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.chmod(LEDGER_PATH, 0o600)
    return row


def _extract_summary(action: str, result: dict) -> dict:
    if not isinstance(result, dict):
        return {}
    if action in ("battle", "startTaskBattle"):
        # Both run through Orchestrator.run_battle-shaped results: the actual
        # reward is nested under "reward", not at the top level.
        nested = result.get("reward")
        if isinstance(nested, dict):
            return nested.get("rewardSummary") or {}
        return {}
    if action == "completeNewbieQuest":
        # Its own shape entirely: {"success": true, "xpGained": N, "nextQuest": M}.
        # No "rewardSummary" at all, so every call was silently recording nothing.
        xp = result.get("xpGained")
        if xp is None:
            return {}
        return {"type": "newbieQuest", "xp": int(xp)}
    if action == "claimTaskReward":
        # Documented in tasks.py as {goldAwarded, allTasksCompleted} — also not
        # "rewardSummary", found while checking every action against the same
        # class of bug the two entries above already had. Never yet fired live
        # (no wallet has finished a full hunt task's kill count), so this was
        # still latent rather than measured missing.
        gold = result.get("goldAwarded")
        if gold is None:
            return {}
        return {"type": "hunt_task", "gold": int(gold)}
    return result.get("rewardSummary") or {}


@dataclass
class Totals:
    gold: int = 0
    xp: int = 0
    battles_won: int = 0
    battles_lost: int = 0
    items: dict = field(default_factory=dict)
    entries: int = 0
    first_ts: int = 0
    last_ts: int = 0

    @property
    def hours(self) -> float:
        if not self.first_ts or self.last_ts <= self.first_ts:
            return 0.0
        return (self.last_ts - self.first_ts) / 3600.0

    @property
    def gold_per_hour(self) -> float:
        return self.gold / self.hours if self.hours else 0.0

    @property
    def xp_per_hour(self) -> float:
        return self.xp / self.hours if self.hours else 0.0

    @property
    def win_rate(self) -> float:
        total = self.battles_won + self.battles_lost
        return self.battles_won / total if total else 0.0


def totals(wallet_id: str | None = None) -> Totals:
    result = Totals()
    if not LEDGER_PATH.exists():
        return result

    for line in LEDGER_PATH.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if wallet_id and row.get("wallet_id") != wallet_id:
            continue

        reward = row.get("reward") or {}
        result.entries += 1
        timestamp = int(row.get("ts", 0))
        if timestamp:
            result.first_ts = min(result.first_ts or timestamp, timestamp)
            result.last_ts = max(result.last_ts, timestamp)

        result.gold += int(reward.get("gold", 0) or 0)
        result.xp += int(reward.get("xp", 0) or 0)
        if reward.get("type") == "battle":
            if reward.get("winner") == "player":
                result.battles_won += 1
            else:
                result.battles_lost += 1
        for item in reward.get("items") or []:
            key = item.get("id", "unknown")
            result.items[key] = result.items.get(key, 0) + int(item.get("quantity", 0) or 0)
    return result


def valued_totals(wallet_id: str | None = None, market=None) -> tuple[Totals, float]:
    """Totals plus the market value of accumulated item drops, when priced."""
    result = totals(wallet_id)
    item_value = market.value_of(result.items) if market else 0.0
    return result, item_value
