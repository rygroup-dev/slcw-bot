"""Server state parsing.

Field names and shapes here were taken from a live capture, not guessed:
`maxEnergy` is a real field, and the battle document reports `maxHP: 130` for a
level-6 character with vitality 3, which confirms the 100 + attribute*10 formula.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

# Firestore integers below this magnitude are seconds, above are milliseconds.
# 10^11 seconds is year 5138; 10^11 ms is 1973. Real timestamps never straddle it.
_MS_THRESHOLD = 10**11


def normalize_timestamp(value: Any) -> int:
    """Return epoch milliseconds for any timestamp shape the server emits.

    Handles ISO-8601 strings, Firestore `{seconds}` / `{_seconds}` maps, and raw
    integers in either seconds or milliseconds. The previous implementation treated
    every bare integer as milliseconds, so an epoch-seconds value resolved to 1970
    and made every activity look expired.
    """
    if value in (None, "", 0):
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number * 1000 if number < _MS_THRESHOLD else number
    if isinstance(value, str):
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    if isinstance(value, dict):
        for key in ("seconds", "_seconds"):
            if key in value:
                nanos = value.get("nanos") or value.get("_nanoseconds") or 0
                return int(value[key]) * 1000 + int(nanos) // 1_000_000
        if "endTime" in value:
            return normalize_timestamp(value["endTime"])
    return 0


def decode_firestore(value: dict) -> Any:
    """Convert one Firestore REST typed value into a plain Python value."""
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    for key in ("stringValue", "booleanValue", "timestampValue"):
        if key in value:
            return value[key]
    if "nullValue" in value:
        return None
    if "mapValue" in value:
        return {k: decode_firestore(v) for k, v in value["mapValue"].get("fields", {}).items()}
    if "arrayValue" in value:
        return [decode_firestore(v) for v in value["arrayValue"].get("values", [])]
    return None


def decode_document(raw: dict) -> dict:
    return {k: decode_firestore(v) for k, v in (raw.get("fields") or {}).items()}


@dataclass
class Activity:
    type: str = ""
    activity_id: str = ""
    start_ms: int = 0
    end_ms: int = 0
    data: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return bool(self.end_ms) and self.end_ms <= _now_ms()

    def seconds_remaining(self) -> float:
        if not self.end_ms:
            return 0.0
        return max(0.0, (self.end_ms - _now_ms()) / 1000.0)


def _now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


@dataclass
class PlayerState:
    level: int = 1
    xp: int = 0
    grade: int = 1
    gold: int = 0
    diamonds: int = 0
    usdt: int = 0
    energy: int = 0
    max_energy: int = 100
    health: int = 0
    mana: int = 0
    attribute_points: int = 0
    newbie_quest: int = 0
    location_id: str = ""
    # Free energy refills are capped at three per day and reset by date, so both
    # halves are needed to know whether any remain.
    free_refills_today: int = 0
    last_free_refill_date: str = ""
    attributes: dict = field(default_factory=dict)
    professions: dict = field(default_factory=dict)
    equipment: dict = field(default_factory=dict)
    claimed_levels: set = field(default_factory=set)
    activity: Activity | None = None
    raw: dict = field(default_factory=dict)

    @property
    def max_health(self) -> int:
        return 100 + int(self.attributes.get("vitality", 3)) * 10

    @property
    def max_mana(self) -> int:
        return 100 + int(self.attributes.get("wisdom", 3)) * 10

    @property
    def health_ratio(self) -> float:
        return self.health / self.max_health if self.max_health else 1.0

    @property
    def mana_ratio(self) -> float:
        return self.mana / self.max_mana if self.max_mana else 1.0

    @property
    def is_busy(self) -> bool:
        return self.activity is not None and not self.activity.is_expired

    FREE_REFILLS_PER_DAY = 3

    def free_refills_left(self, today: str | None = None) -> int:
        """Refills remaining today. The counter resets when the date changes."""
        today = today or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        used = self.free_refills_today if self.last_free_refill_date == today else 0
        return max(0, self.FREE_REFILLS_PER_DAY - used)

    def unclaimed_levels(self) -> list[int]:
        return [lvl for lvl in range(1, self.level + 1) if lvl not in self.claimed_levels]


def parse_player(doc: dict) -> PlayerState:
    activity_raw = doc.get("activity")
    activity = None
    if isinstance(activity_raw, dict) and activity_raw:
        activity = Activity(
            type=str(activity_raw.get("type", "")),
            activity_id=str(activity_raw.get("activityId", "")),
            start_ms=normalize_timestamp(activity_raw.get("startTime")),
            end_ms=normalize_timestamp(activity_raw.get("endTime")),
            data=activity_raw.get("data") or {},
        )

    attributes = doc.get("attributes") or {}
    max_health = 100 + int(attributes.get("vitality", 3)) * 10
    max_mana = 100 + int(attributes.get("wisdom", 3)) * 10

    return PlayerState(
        level=int(doc.get("level", 1) or 1),
        xp=int(doc.get("xp", 0) or 0),
        grade=int(doc.get("grade", 1) or 1),
        gold=int(doc.get("balance", 0) or 0),
        diamonds=int(doc.get("premium_balance", 0) or 0),
        usdt=int(doc.get("usdt_balance", 0) or 0),
        energy=int(doc.get("energy", 0) or 0),
        max_energy=int(doc.get("maxEnergy", 100) or 100),
        health=int(doc.get("currentHealth", max_health) or 0),
        mana=int(doc.get("currentMana", max_mana) or 0),
        attribute_points=int(doc.get("attributePoints", 0) or 0),
        newbie_quest=int(doc.get("newbieQuest", 0) or 0),
        location_id=str(doc.get("currentLocationId") or ""),
        free_refills_today=int(doc.get("freeEnergyRefillsToday", 0) or 0),
        last_free_refill_date=str(doc.get("lastFreeEnergyRefillDate") or ""),
        attributes=attributes,
        professions=doc.get("professions") or {},
        equipment=doc.get("equipment") or {},
        claimed_levels={int(x) for x in (doc.get("claimedInitialRewardsV2") or [])},
        activity=activity,
        raw=doc,
    )
