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
ENV_PATH = ROOT / ".env"

PROJECT = "slcw-15253244-abbca"
REGION = "us-central1"
FUNCTION_BASE = f"https://{REGION}-{PROJECT}.cloudfunctions.net"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
SECURETOKEN_URL = "https://securetoken.googleapis.com/v1/token"
IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
APP_ORIGIN = "https://app.slcw.xyz"


def parse_env_text(text: str) -> dict:
    """Read KEY=value pairs, ignoring comments and blank lines."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def persist_overrides(updates: dict, path: Path | None = None) -> None:
    """Write `updates` into .env, in place, and into the live environment.

    The Telegram economy switches used to be a dataclasses.replace on the
    in-memory Config and nothing else, so gold-mode, its duration, auto-travel
    and dry-run all reverted to whatever .env said the next time the service
    restarted — the operator's last instruction was silently discarded.

    Rewriting the file wholesale would drop comments and any key this version
    does not know about, so each key is edited on its own line and anything
    unrecognised is left exactly as it was found.
    """
    path = path or ENV_PATH
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)

    # systemd re-reads .env on restart, but this process must not keep serving
    # the value the operator just changed.
    for key, value in updates.items():
        os.environ[key] = str(value)


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
    # The daily offline window per wallet (sleep_anchor_hour, sleep_hours) is
    # drawn once at wallet creation and stored per-wallet in the vault
    # (slcw.vault), not read from here — SLCW_SLEEP_MIN_HOURS/MAX_HOURS used
    # to exist as config fields but nothing ever read them.

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
    # Gold-mode farming runs 1-8 hours and spends no energy. It also locks the
    # wallet for that whole time — no levelling, no chests, no task claims — and
    # the gold-per-hour score cannot see that cost, so it is switchable.
    farming_gold: bool = field(default_factory=lambda: _bool("SLCW_FARMING_GOLD", True))
    farming_gold_hours: int = field(default_factory=lambda: _int("SLCW_FARMING_GOLD_HOURS", 8))
    # Keep this much gold in reserve; gold-funded actions may not dip below it.
    gold_reserve: int = field(default_factory=lambda: _int("SLCW_GOLD_RESERVE", 500))
    # Optional home location the fleet travels back to when idle elsewhere.
    home_location: str = field(default_factory=lambda: os.environ.get("SLCW_HOME_LOCATION", ""))
    # Let the engine relocate between gathering sites and workshop cities.
    auto_travel: bool = field(default_factory=lambda: _bool("SLCW_AUTO_TRAVEL", True))

    # --- clans ---------------------------------------------------------
    # Submitting quest resources spends raw drops the market has no bids for,
    # so it is on by default. Donating gold moves funds into a treasury this
    # operator may not control, so that half is opt-in and separate.
    clan_enabled: bool = field(default_factory=lambda: _bool("SLCW_CLAN_ENABLED", True))
    clan_donate_gold: bool = field(
        default_factory=lambda: _bool("SLCW_CLAN_DONATE_GOLD", False))
    # Gold kept back from any donation, on top of the general gold reserve.
    clan_gold_reserve: int = field(
        default_factory=lambda: _int("SLCW_CLAN_GOLD_RESERVE", 5000))
    # A destination must beat staying put by this multiple before travelling.
    # Travel time is dead time, so a marginal gain does not justify the trip.
    travel_margin: float = field(default_factory=lambda: _float("SLCW_TRAVEL_MARGIN", 1.35))
    # Item drops worth at least this much (at best bid) raise a Telegram alert.
    rich_drop_gold: int = field(default_factory=lambda: _int("SLCW_RICH_DROP_GOLD", 2000))
    # Which attribute build to pursue: sustain, balanced, or damage.
    build: str = field(default_factory=lambda: os.environ.get("SLCW_BUILD", "sustain"))
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
