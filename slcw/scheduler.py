"""Per-wallet timing.

The old deployment ran a systemd timer with OnUnitActiveSec=5min, which produced a
perfectly regular cadence in the journal — 04:20:58, 04:25:58, 04:30:58, 04:35:58 —
because the in-engine sleep shifted every run by the same amount rather than
breaking the period. Here each wallet computes its own next wake time from server
state, so no two intervals match and the fleet never moves in lockstep.
"""
from __future__ import annotations

import datetime as _dt
import math
import random
from dataclasses import dataclass

from .config import Config


def reaction_delay(config: Config, rng: random.Random | None = None) -> float:
    """Draw a human-shaped delay between an event and our response to it.

    Log-normal rather than uniform: most reactions cluster near the median, with a
    long tail for the times a person walks away from the screen.
    """
    rng = rng or random
    median = max(1.0, config.reaction_median_seconds)
    # sigma 0.85 gives roughly a 4x spread between the 10th and 90th percentile.
    value = rng.lognormvariate(math.log(median), 0.85)
    return min(max(value, config.reaction_min_seconds), config.reaction_max_seconds)


def idle_delay(config: Config, rng: random.Random | None = None) -> float:
    rng = rng or random
    return rng.uniform(config.idle_min_seconds, config.idle_max_seconds)


@dataclass
class SleepWindow:
    """A wallet's daily offline period, stable across restarts."""

    anchor_hour: int
    hours: float

    def contains(self, moment: _dt.datetime) -> bool:
        start = self.anchor_hour
        end = (self.anchor_hour + self.hours) % 24
        hour = moment.hour + moment.minute / 60.0
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def seconds_until_wake(self, moment: _dt.datetime) -> float:
        if not self.contains(moment):
            return 0.0
        end_hour = (self.anchor_hour + self.hours) % 24
        hour = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
        delta = (end_hour - hour) % 24
        return delta * 3600.0


def sleep_window_for(wallet: dict) -> SleepWindow:
    return SleepWindow(
        anchor_hour=int(wallet.get("sleep_anchor_hour", 3)),
        hours=float(wallet.get("sleep_hours", 7.0)),
    )


def next_wake_seconds(config: Config, wallet: dict, state, now: _dt.datetime | None = None,
                      rng: random.Random | None = None) -> tuple[float, str]:
    """Return (seconds_to_sleep, reason).

    Wake time is derived from the server's own activity clock when one is running,
    so the bot responds to the game rather than to the wall clock.
    """
    rng = rng or random
    now = now or _dt.datetime.now(_dt.timezone.utc)

    window = sleep_window_for(wallet)
    if window.contains(now):
        remaining = window.seconds_until_wake(now)
        # Wake a little off the exact boundary so the account does not resume at the
        # same minute every day.
        return remaining + rng.uniform(60, 900), "sleep window"

    if state is not None and state.is_busy and state.activity is not None:
        remaining = state.activity.seconds_remaining()
        return remaining + reaction_delay(config, rng), f"{state.activity.type} in progress"

    return idle_delay(config, rng), "idle poll"
