"""Raising a character's grade, which is what lifts its level ceiling.

A character cannot pass level 15 x grade, and every wallet in this fleet is
grade 1 at level 15 — so every point of XP any of them earns is discarded. The
way out is the Greyholm Altar, and it was hiding in plain sight: the newbie
quest chain's last step pays four Imperial Seals, fifteen wallets were already
holding them, and grade 2 wants five.

Everything here is read from the game's own bundles rather than inferred:
app/evolution carries the requirement table and app/imperialshop carries the
price curve. Seals are spent irreversibly, so nothing in this module guesses.
"""
from __future__ import annotations

# The altar and the shop are both in Greyholm, and both refuse to work anywhere
# else. Entry is `accessType: "paid_50"` — fifty gold, measured 2026-08-22.
CITADEL = "city_17"
SEAL_ITEM = "imperial_seal"

# grade -> (level required, seals required). Verbatim from app/evolution.
# Levels go up by 15 and seals by five-fold, and both are still written out:
# a formula that fits six rows is a guess about the seventh, and the seventh
# costs 15,625 seals.
REQUIREMENTS = {
    1: (0, 0),
    2: (15, 5),
    3: (30, 25),
    4: (45, 125),
    5: (60, 625),
    6: (75, 3_125),
    7: (90, 15_625),
}

MAX_GRADE = max(REQUIREMENTS)

# Imperial shop pricing, from app/imperialshop. The seal's base is 3,500 gold,
# and the city warehouse moves it: at empty it doubles, at full it halves.
SEAL_BASE_PRICE = 3_500

# Greyholm is `accessType: "paid_50"`, measured 2026-08-22. The other setting
# the client knows about charges 1,000, so budget for that rather than for the
# cheaper case we happen to be looking at.
CITY_ENTRY_FEE = 1_000


def requirement(grade: int) -> tuple[int, int] | None:
    """(level, seals) needed to *be* this grade, or None if there is no such grade."""
    return REQUIREMENTS.get(int(grade))


def next_grade(grade: int) -> int | None:
    step = int(grade or 1) + 1
    return step if step in REQUIREMENTS else None


def seals_needed(grade: int, held: int) -> int:
    """Seals still to find before this character can take the next grade."""
    step = next_grade(grade)
    if step is None:
        return 0
    _, seals = REQUIREMENTS[step]
    return max(0, seals - max(0, int(held or 0)))


def can_evolve(level: int, grade: int, seals: int) -> bool:
    step = next_grade(grade)
    if step is None:
        return False
    need_level, need_seals = REQUIREMENTS[step]
    return int(level) >= need_level and int(seals or 0) >= need_seals


def seal_price(stock: int, capacity: int = 0) -> int:
    """What one seal costs at a city holding this much of it.

    The curve is the client's, transcribed: below half capacity the multiplier
    runs from 2 down to 1, above it from 1 down to 0.5, and it never goes under
    half the base price.
    """
    capacity = int(capacity or 0)
    if capacity <= 0:
        # Not read, rather than empty. Treating it as empty would quote double.
        return SEAL_BASE_PRICE
    half = max(capacity / 2, 1)
    stock = max(0, int(stock or 0))
    if stock <= half:
        factor = 2 - stock / half
    else:
        factor = 1 - 0.5 * min((stock - half) / half, 1)
    return int(-(-SEAL_BASE_PRICE * max(factor, 0.5) // 1))
