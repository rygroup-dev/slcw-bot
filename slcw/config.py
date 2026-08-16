"""Typed configuration.

Every key documented here is actually read by the code. The previous .env carried
SLCW_MAX_ACTIONS_PER_HOUR, SLCW_MIN_JITTER_SECONDS, SLCW_MAX_JITTER_SECONDS and
SLCW_MAX_ERRORS that no module ever consulted, so tuning them changed nothing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

PROJECT = "slcw-15253244-abbca"
REGION = "us-central1"
FUNCTION_BASE = f"https://{REGION}-{PROJECT}.cloudfunctions.net"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
SECURETOKEN_URL = "https://securetoken.googleapis.com/v1/token"
IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
APP_ORIGIN = "https://app.slcw.xyz"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # --- master switches -------------------------------------------------
    enabled: bool = field(default_factory=lambda: _bool("SLCW_ENABLED", False))
    dry_run: bool = field(default_factory=lambda: _bool("SLCW_DRY_RUN", True))

    # --- pacing ----------------------------------------------------------
    # Reaction delay after a server activity ends, log-normal shaped.
    reaction_median_seconds: float = field(
        default_factory=lambda: _float("SLCW_REACTION_MEDIAN_SECONDS", 90.0))
    reaction_min_seconds: float = field(
        default_factory=lambda: _float("SLCW_REACTION_MIN_SECONDS", 15.0))
    reaction_max_seconds: float = field(
        default_factory=lambda: _float("SLCW_REACTION_MAX_SECONDS", 480.0))
    # Poll spacing when a wallet has nothing pending.
    idle_min_seconds: float = field(default_factory=lambda: _float("SLCW_IDLE_MIN_SECONDS", 240.0))
    idle_max_seconds: float = field(default_factory=lambda: _float("SLCW_IDLE_MAX_SECONDS", 900.0))
    # Daily offline window per wallet.
    sleep_min_hours: float = field(default_factory=lambda: _float("SLCW_SLEEP_MIN_HOURS", 6.0))
    sleep_max_hours: float = field(default_factory=lambda: _float("SLCW_SLEEP_MAX_HOURS", 9.0))

    # --- resilience ------------------------------------------------------
    max_consecutive_errors: int = field(default_factory=lambda: _int("SLCW_MAX_ERRORS", 3))
    http_max_attempts: int = field(default_factory=lambda: _int("SLCW_HTTP_MAX_ATTEMPTS", 4))
    http_backoff_base: float = field(default_factory=lambda: _float("SLCW_HTTP_BACKOFF_BASE", 1.6))
    http_timeout_seconds: float = field(default_factory=lambda: _float("SLCW_HTTP_TIMEOUT", 30.0))

    # --- gameplay policy -------------------------------------------------
    rest_hp_ratio: float = field(default_factory=lambda: _float("SLCW_REST_HP_RATIO", 0.55))
    rest_mp_ratio: float = field(default_factory=lambda: _float("SLCW_REST_MP_RATIO", 0.25))
    battle_max_turns: int = field(default_factory=lambda: _int("SLCW_BATTLE_MAX_TURNS", 12))
    market_ttl_seconds: int = field(default_factory=lambda: _int("SLCW_MARKET_TTL_SECONDS", 1800))
    # Gold-mode farming runs 1-8 hours and spends no energy.
    farming_gold_hours: int = field(default_factory=lambda: _int("SLCW_FARMING_GOLD_HOURS", 8))
    # Keep this much gold in reserve; gold-funded actions may not dip below it.
    gold_reserve: int = field(default_factory=lambda: _int("SLCW_GOLD_RESERVE", 500))
    # Optional home location the fleet travels back to when idle elsewhere.
    home_location: str = field(default_factory=lambda: os.environ.get("SLCW_HOME_LOCATION", ""))
    # Let the engine relocate between gathering sites and workshop cities.
    auto_travel: bool = field(default_factory=lambda: _bool("SLCW_AUTO_TRAVEL", True))
    # A destination must beat staying put by this multiple before travelling.
    # Travel time is dead time, so a marginal gain does not justify the trip.
    travel_margin: float = field(default_factory=lambda: _float("SLCW_TRAVEL_MARGIN", 1.35))
    # Item drops worth at least this much (at best bid) raise a Telegram alert.
    rich_drop_gold: int = field(default_factory=lambda: _int("SLCW_RICH_DROP_GOLD", 2000))
    # Solana RPC for wallet funding. The public endpoint rate-limits readily, so
    # point this at a dedicated provider before sending to many wallets.
    solana_rpc: str = field(default_factory=lambda: os.environ.get(
        "SLCW_SOLANA_RPC", "https://api.mainnet-beta.solana.com"))

    # --- credentials -----------------------------------------------------
    firebase_api_key: str = field(default_factory=lambda: os.environ.get("SLCW_FIREBASE_API_KEY", ""))
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))

    def validate(self) -> list[str]:
        """Return human-readable problems; empty list means good to run."""
        problems = []
        if not self.firebase_api_key:
            problems.append("SLCW_FIREBASE_API_KEY is unset — auth cannot complete")
        if not self.telegram_token:
            problems.append("TELEGRAM_BOT_TOKEN is unset — control plane disabled")
        if not self.telegram_chat_id:
            problems.append("TELEGRAM_CHAT_ID is unset — control plane would accept nobody")
        if self.reaction_min_seconds >= self.reaction_max_seconds:
            problems.append("SLCW_REACTION_MIN_SECONDS must be below SLCW_REACTION_MAX_SECONDS")
        if self.idle_min_seconds >= self.idle_max_seconds:
            problems.append("SLCW_IDLE_MIN_SECONDS must be below SLCW_IDLE_MAX_SECONDS")
        if not 0 < self.rest_hp_ratio < 1:
            problems.append("SLCW_REST_HP_RATIO must be between 0 and 1")
        if not 1 <= self.farming_gold_hours <= 8:
            problems.append("SLCW_FARMING_GOLD_HOURS must be between 1 and 8")
        if self.travel_margin < 1.0:
            problems.append("SLCW_TRAVEL_MARGIN below 1.0 would travel for a loss")
        return problems


def load() -> Config:
    return Config()
