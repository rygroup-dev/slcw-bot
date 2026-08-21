"""Clans: membership, the daily treasury donation, and clan quest resources.

Reverse-engineered live on 2026-08-21. None of this is in the frontend bundle —
the clan pages are server-rendered, so the callables were found by sweeping the
deployed function namespace and their argument shapes by reading which rejection
came back. What follows is the measured contract, not a guess:

    searchClans          {query}                                 -> {clans:[...]}
    getClanMembers       {clanId}                                -> {members:[...]}
    applyClan            {clanId}                                -> {applicationId}
    cancelApplication    {applicationId}
    leaveClan            {}
    createClan           {name, tag, description, languages}     costs 20,000 gold
    makeDonation         {amount, currency}                      currency: gold|slcw
    submitQuestResources {clanId, questId, itemId, amount}
    generateClanQuest    {clanId}                                officer or leader

Firestore carries the state the callables do not return: `clans/{id}` holds level,
xp, treasury and memberCount; `clans/{id}/members/{uid}` holds role, dkp, joinedAt
and lastDonationAt; `clans/{id}/quests/{id}` holds the active quest's requirements
and per-member contributions.

Two economics matter for an automated fleet, and they point opposite ways.

A treasury donation converts gold at 1,000 gold = 1 DKP, once per day, and the
gold lands in a pot that the clan leader — not this operator — decides how to
distribute. That is fund movement out of the wallet in exchange for a claim that
someone else honours, so it is off unless the operator turns it on deliberately.

Submitting quest resources spends raw drops instead. Raw materials have no bids at
all on the market (see market.py), so items like frostfang and glowingspore are
otherwise dead weight in a bounded inventory. Turning them into DKP and clan XP is
the one clan action that costs the fleet nothing it could have sold.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

# Measured from the client's own copy: "Rate: 1,000 Gold = 1 DKP" and
# "1 $SLCW = 50 DKP", with "Minimum donation: 1,000 Gold or 1 $SLCW".
GOLD_PER_DKP = 1_000
SLCW_PER_DKP = 50
MIN_GOLD_DONATION = 1_000
MIN_SLCW_DONATION = 1

# "You have already donated today. Reset is daily at 00:00 UTC."
DONATION_RESET_UTC_HOUR = 0

# "As a recruit, you have a 3-day probation period. During this time, you cannot
# make donations." Roles rank initiate < member < officer < leader.
PROBATION_DAYS = 3
ROLE_INITIATE = "initiate"
ROLES = ("initiate", "member", "officer", "leader")

CREATE_CLAN_GOLD = 20_000


@dataclass
class ClanQuest:
    """An active clan quest: donate N of each listed item, split a DKP pool."""

    quest_id: str = ""
    requirements: list = field(default_factory=list)  # [{itemId, required, collected}]
    reward_dkp_pool: int = 0
    reward_clan_xp: int = 0
    completed: bool = False

    def outstanding(self) -> dict:
        """How many of each item the quest still needs."""
        short = {}
        for req in self.requirements:
            item = str(req.get("itemId") or "")
            if not item:
                continue
            missing = int(req.get("required", 0) or 0) - int(req.get("collected", 0) or 0)
            if missing > 0:
                short[item] = missing
        return short


@dataclass
class ClanMembership:
    """This wallet's own standing inside its clan."""

    clan_id: str = ""
    role: str = ""
    dkp: int = 0
    joined_at_ms: int = 0
    last_donation_ms: int = 0

    @property
    def is_member(self) -> bool:
        return bool(self.clan_id)

    def on_probation(self, now: _dt.datetime | None = None) -> bool:
        """Recruits cannot donate for their first three days."""
        if self.role != ROLE_INITIATE or not self.joined_at_ms:
            return False
        now = now or _dt.datetime.now(_dt.timezone.utc)
        joined = _dt.datetime.fromtimestamp(self.joined_at_ms / 1000, _dt.timezone.utc)
        return (now - joined).days < PROBATION_DAYS

    def donated_today(self, now: _dt.datetime | None = None) -> bool:
        """The donation window resets at 00:00 UTC, not 24h after the last one."""
        if not self.last_donation_ms:
            return False
        now = now or _dt.datetime.now(_dt.timezone.utc)
        last = _dt.datetime.fromtimestamp(self.last_donation_ms / 1000, _dt.timezone.utc)
        return last.date() == now.date()

    def can_donate(self, now: _dt.datetime | None = None) -> bool:
        return (self.is_member and not self.on_probation(now)
                and not self.donated_today(now))


def donation_dkp(amount: int, currency: str = "gold") -> int:
    """DKP a donation of this size returns."""
    if currency == "slcw":
        return int(amount) * SLCW_PER_DKP
    return int(amount) // GOLD_PER_DKP


def minimum_donation(currency: str = "gold") -> int:
    return MIN_SLCW_DONATION if currency == "slcw" else MIN_GOLD_DONATION


def affordable_donation(gold: int, reserve: int = 0) -> int:
    """Largest whole-DKP gold donation that leaves the reserve untouched.

    Donating a part-DKP remainder would hand over gold that buys nothing, so the
    amount is always rounded down to a multiple of the rate.
    """
    spendable = max(0, int(gold) - int(reserve))
    return (spendable // GOLD_PER_DKP) * GOLD_PER_DKP


def submittable(quest: ClanQuest, holdings: dict) -> tuple[str, int]:
    """Pick the item to submit and how many, or ("", 0) when there is nothing.

    Preference goes to whatever the wallet holds most of among the items the
    quest still needs: a bounded inventory is worth more emptied of the stack
    that is crowding it, and every listed item counts the same toward the pool.
    """
    outstanding = quest.outstanding()
    if not outstanding or quest.completed:
        return "", 0

    best_item, best_amount = "", 0
    for item, missing in outstanding.items():
        held = int((holdings or {}).get(item, 0) or 0)
        amount = min(held, missing)
        if amount > best_amount:
            best_item, best_amount = item, amount
    return best_item, best_amount


def parse_quest(doc: dict | None, quest_id: str = "") -> ClanQuest | None:
    if not isinstance(doc, dict) or not doc:
        return None
    return ClanQuest(
        quest_id=quest_id or str(doc.get("questId") or ""),
        requirements=list(doc.get("requirements") or []),
        reward_dkp_pool=int(doc.get("rewardDkpPool", 0) or 0),
        reward_clan_xp=int(doc.get("rewardClanXp", 0) or 0),
        completed=doc.get("completedAt") is not None,
    )


def parse_membership(clan_id: str, member_doc: dict | None) -> ClanMembership:
    doc = member_doc or {}
    return ClanMembership(
        clan_id=clan_id or "",
        role=str(doc.get("role") or ""),
        dkp=int(doc.get("dkp", 0) or 0),
        joined_at_ms=_ms(doc.get("joinedAt")),
        last_donation_ms=_ms(doc.get("lastDonationAt")),
    )


def _ms(value) -> int:
    """Timestamps arrive as epoch millis or as an ISO string, depending on path."""
    from .model import normalize_timestamp
    return normalize_timestamp(value) or 0
