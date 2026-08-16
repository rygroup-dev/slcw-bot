"""Level progression.

Levelling is a manual click in the profile, not something the server does when
XP accrues, so an unattended account simply stops progressing. The call is:

    buyLevel({booster, cardIndex}) -> {levelGain, pointsGain, pool, newLevel}

The name is misleading. Only `booster: "diamond"` costs anything — the client
charges `99 * (level + 1)` diamonds in that branch alone. With `booster: "none"`
the level is paid for with XP and nothing else, which is why this is allowed
while every other `buy*` call is not.

Two gates apply: enough XP for the next level, and level below 15 x grade.
`cardIndex` picks one of the reward cards; the pool is returned afterwards.
"""
from __future__ import annotations

# XP required per level, read from the profile bundle. Index 0 is level 1.
XP_TABLE = (
    50, 100, 150, 200, 300, 400, 500, 650, 800, 1_000,
    1_200, 1_400, 1_600, 1_800, 2_000, 2_500, 3_000, 3_500, 4_000, 5_000,
    6_000, 7_000, 8_000, 9_000, 10_000, 12_000, 14_000, 16_000, 18_000, 20_000,
    22_000, 24_000, 26_000, 28_000, 30_000, 35_000, 40_000, 45_000, 50_000, 55_000,
    60_000, 65_000, 70_000, 75_000, 80_000, 90_000, 100_000, 110_000, 120_000, 130_000,
    140_000, 150_000, 160_000, 170_000, 180_000, 200_000, 220_000, 240_000, 260_000,
    280_000, 300_000, 320_000, 340_000, 360_000, 380_000, 410_000, 440_000, 470_000,
    500_000, 550_000, 600_000, 650_000, 700_000, 750_000, 800_000, 850_000, 900_000,
    950_000, 1_000_000, 1_050_000, 1_100_000, 1_150_000, 1_200_000, 1_250_000,
    1_300_000, 1_350_000,
)

# Beyond the table the client asks for a flat two million.
XP_BEYOND_TABLE = 2_000_000

# A character cannot pass this multiple of its grade.
LEVEL_PER_GRADE = 15

FREE_BOOSTER = "none"


def xp_required(level: int) -> int:
    """XP needed to advance from `level` to the next one."""
    if level <= 0:
        return 50
    if level <= len(XP_TABLE):
        return XP_TABLE[level - 1]
    return XP_BEYOND_TABLE


def level_cap(grade: int) -> int:
    return LEVEL_PER_GRADE * max(1, int(grade or 1))


def at_grade_cap(level: int, grade: int) -> bool:
    return level >= level_cap(grade)


def can_level_up(level: int, grade: int, xp: int) -> bool:
    """Whether a free level-up is available right now."""
    if at_grade_cap(level, grade):
        return False
    return xp >= xp_required(level)


def progress(level: int, xp: int) -> float:
    """Fraction of the way to the next level, capped at 1."""
    needed = xp_required(level)
    return min(1.0, xp / needed) if needed else 0.0


def blocked_reason(level: int, grade: int, xp: int) -> str:
    """Why a level-up is unavailable, empty when it is available."""
    if at_grade_cap(level, grade):
        return (f"grade cap: level {level} is the ceiling for grade {grade} "
                f"(raising grade consumes imperial seals)")
    needed = xp_required(level)
    if xp < needed:
        return f"{needed - xp:,} more XP needed ({xp:,}/{needed:,})"
    return ""


def payload(card_index: int = 0) -> dict:
    """Arguments for a free level-up.

    The booster is pinned to "none" here rather than passed in: the diamond
    branch is the only thing that costs premium currency, and it must not be
    reachable by accident.
    """
    return {"booster": FREE_BOOSTER, "cardIndex": int(card_index)}
