"""Fleet daemon: one independent worker thread per wallet.

Replaces the oneshot systemd timer. Each worker sleeps on its own schedule, keeps
its own session, and trips its own circuit breaker — a failing wallet no longer
pauses the entire fleet, which is what the old global `data/paused` file did.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field

from . import ledger, market as market_mod, scheduler, tasks
from .api import GameApi
from .auth import AuthError, SessionManager
from .config import DATA, Config
from .market import MarketSnapshot
from .orchestrator import Orchestrator
from .transport import ApiError, Transport, TransportError

FLEET_STATE = DATA / "fleet_state.json"


@dataclass
class WalletStatus:
    wallet_id: str
    nickname: str = ""
    public_key: str = ""
    paused: bool = False
    pause_reason: str = ""
    consecutive_errors: int = 0
    last_action: str = ""
    last_reason: str = ""
    last_error: str = ""
    last_run_ts: int = 0
    next_wake_ts: int = 0
    next_wake_reason: str = ""
    rationale: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    holdings: dict = field(default_factory=dict)
    logins: int = 0
    refreshes: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class Fleet:
    """Owns wallet worker threads and the shared market snapshot."""

    def __init__(self, config: Config, vault, notifier):
        from .notify import Alerts

        self.config = config
        self.vault = vault
        self.notifier = notifier
        self.alerts = Alerts(notifier)
        self.sessions = SessionManager(config)
        self.status: dict = {}
        self.market: MarketSnapshot = market_mod.load_snapshot()
        self.force_flags: dict = {}
        # Most recent hunt-task status seen, for the Telegram view.
        self.last_task_status = None
        self._threads: dict = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._wake_events: dict = {}

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        for wallet in self.vault.wallets():
            self.ensure_worker(wallet)

    def ensure_worker(self, wallet: dict) -> None:
        wallet_id = wallet["id"]
        if wallet_id in self._threads and self._threads[wallet_id].is_alive():
            return
        with self._lock:
            self.status.setdefault(wallet_id, WalletStatus(
                wallet_id=wallet_id,
                nickname=wallet.get("nickname", ""),
                public_key=wallet.get("public_key", ""),
                paused=not wallet.get("enabled", True),
            ))
            self._wake_events.setdefault(wallet_id, threading.Event())
        thread = threading.Thread(
            target=self._worker, args=(wallet_id,), name=f"slcw-{wallet_id}", daemon=True)
        self._threads[wallet_id] = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        for event in self._wake_events.values():
            event.set()

    # --- controls --------------------------------------------------------
    def pause(self, wallet_id: str, reason: str = "manual") -> None:
        status = self.status.get(wallet_id)
        if status:
            status.paused = True
            status.pause_reason = reason
        self.persist()

    def resume(self, wallet_id: str) -> None:
        status = self.status.get(wallet_id)
        if status:
            status.paused = False
            status.pause_reason = ""
            # Clearing the counter is what makes resume actually work. Previously
            # the count stayed at its trip value, so the next error re-paused
            # immediately.
            status.consecutive_errors = 0
        self.wake(wallet_id)
        self.persist()

    def pause_all(self, reason: str = "manual") -> None:
        for wallet_id in list(self.status):
            self.pause(wallet_id, reason)

    def resume_all(self) -> None:
        for wallet_id in list(self.status):
            self.resume(wallet_id)

    def wake(self, wallet_id: str) -> None:
        event = self._wake_events.get(wallet_id)
        if event:
            event.set()

    def force_cycle(self, wallet_id: str | None = None) -> None:
        targets = [wallet_id] if wallet_id else list(self.status)
        for target in targets:
            self.force_flags[target] = True
            self.wake(target)

    # --- worker ----------------------------------------------------------
    def _worker(self, wallet_id: str) -> None:
        rng = random.Random(f"{wallet_id}-{time.time()}")
        event = self._wake_events[wallet_id]

        # Stagger initial starts so wallets never begin their first cycle together.
        self._sleep(event, rng.uniform(3, 45))

        while not self._stop.is_set():
            status = self.status.get(wallet_id)
            wallet = self.vault.get(wallet_id) if self.vault.is_unlocked else None

            if wallet is None or status is None:
                self._sleep(event, 30)
                continue

            if status.paused and not self.force_flags.pop(wallet_id, False):
                self._sleep(event, 60)
                continue

            delay, reason = self._run_cycle(wallet, status, rng)
            status.next_wake_ts = int(time.time() + delay)
            status.next_wake_reason = reason
            self.persist()
            self._sleep(event, delay)

    def _run_cycle(self, wallet: dict, status: WalletStatus,
                   rng: random.Random) -> tuple[float, str]:
        transport = Transport.for_wallet(self.config, wallet)
        try:
            before = len(self.sessions._sessions)
            session = self.sessions.get(wallet, transport)
            status.logins = getattr(session, "logged_in_at", 0) and status.logins or status.logins
            status.refreshes = session.refresh_count

            api = GameApi(transport)

            # A freshly generated account has no game profile until the
            # initializers run. Wallets created before this ran were flagged
            # onboarded=False and then never onboarded by anything.
            if not wallet.get("onboarded", True):
                results = api.onboard(session)
                self.vault.update(wallet["id"], onboarded=True)
                status.last_action = "onboard"
                self.notifier.send(
                    f"🌱 <b>{wallet['id']}</b> ({wallet.get('nickname', '')}) "
                    f"selesai onboarding\n"
                    f"<code>{', '.join(sorted(results))}</code>")

            state = api.get_player(session)
            self.refresh_market(api, session)

            # Refining decisions need to know what the account is actually holding.
            try:
                holdings = api.get_holdings(session)
            except (TransportError, ApiError):
                holdings = {}

            # Hunt tasks only exist from level 10, so below that the call is
            # a guaranteed round trip for nothing.
            task_status = None
            if state.level >= tasks.MIN_LEVEL:
                try:
                    task_status = api.get_task_status(session)
                except (TransportError, ApiError):
                    task_status = None

            if task_status is not None:
                self.last_task_status = task_status

            orchestrator = Orchestrator(config=self.config, api=api, rng=rng)
            decision = orchestrator.decide_and_act(
                wallet, session, state, self.market, holdings, task_status)

            status.last_run_ts = int(time.time())
            status.last_action = decision.action
            status.last_reason = decision.reason
            status.rationale = decision.rationale_lines()
            status.state = {
                "level": state.level, "xp": state.xp, "gold": state.gold,
                "grade": state.grade, "attribute_points": state.attribute_points,
                "diamonds": state.diamonds, "usdt": state.usdt,
                "energy": state.energy, "max_energy": state.max_energy,
                "health": state.health, "max_health": state.max_health,
                "mana": state.mana, "max_mana": state.max_mana,
                "location": state.location_id,
                "activity": state.activity.type if state.activity else "idle",
                "activity_remaining_s": (
                    state.activity.seconds_remaining() if state.activity else 0),
                "free_refills_left": state.free_refills_left(),
            }
            status.holdings = holdings

            if decision.error:
                self._register_error(status, decision.error)
            else:
                status.consecutive_errors = 0
                status.last_error = ""
                if decision.result and not decision.dry_run:
                    ledger.record(wallet["id"], decision.action, decision.result)

            # Re-read the clock from the action we just took so the next wake time
            # follows the server, not our own cadence.
            fresh = api.get_player(session)
            self._notify_progress(status, state, fresh, decision)
            return scheduler.next_wake_seconds(self.config, wallet, fresh, rng=rng)

        except (AuthError, TransportError, ApiError) as exc:
            self.sessions.invalidate(wallet["id"])
            self._register_error(status, f"{type(exc).__name__}: {exc}")
            return scheduler.idle_delay(self.config, rng), "error backoff"
        except Exception as exc:  # never let one wallet kill its thread
            self._register_error(status, f"unexpected {type(exc).__name__}: {exc}")
            return scheduler.idle_delay(self.config, rng), "error backoff"
        finally:
            transport.close()

    def _register_error(self, status: WalletStatus, message: str) -> None:
        status.consecutive_errors += 1
        status.last_error = message
        if "AuthError" in message:
            self.alerts.auth_failure(status.wallet_id, message)
        if status.consecutive_errors >= self.config.max_consecutive_errors:
            status.paused = True
            status.pause_reason = f"circuit breaker after {status.consecutive_errors} errors"
            self.alerts.circuit_breaker(status.wallet_id, status.nickname, message)

    def _notify_progress(self, status: WalletStatus, before, after, decision) -> None:
        """Push only the events an operator would want to be interrupted for."""
        if after.level > before.level:
            self.alerts.level_up(status.wallet_id, after.level)

        if decision.action == "blocked" and decision.error:
            self.alerts.guardrail(status.wallet_id, decision.error)

        if decision.action == "idle" and "no profitable action" in decision.reason:
            self.alerts.low_energy_idle(status.wallet_id)
        else:
            self.alerts.clear(f"idle:{status.wallet_id}")

        summary = (decision.result or {}).get("rewardSummary") or {}
        nested = (decision.result or {}).get("reward") or {}
        if isinstance(nested, dict):
            summary = summary or nested.get("rewardSummary") or {}
        for item in summary.get("items") or []:
            bid = self.market.best_bid(item.get("id", "")) or 0
            quantity = int(item.get("quantity", 0) or 0)
            if bid * quantity >= self.config.rich_drop_gold:
                self.alerts.rich_drop(status.wallet_id, item["id"], quantity, bid * quantity)

    def refresh_market(self, api, session) -> None:
        if self.market.is_fresh(self.config.market_ttl_seconds):
            return
        try:
            orders = api.query_all(session, "blackmarket_orders")
        except (TransportError, ApiError):
            return
        if not orders:
            return
        self.market = market_mod.build_snapshot(orders)
        market_mod.save_snapshot(self.market)
        crossed = self.market.crossed()
        if crossed:
            self.alerts.crossed_market(crossed)

    # --- persistence -----------------------------------------------------
    def persist(self) -> None:
        payload = {
            "updated_at": int(time.time()),
            "dry_run": self.config.dry_run,
            "enabled": self.config.enabled,
            "unlocked": self.vault.is_unlocked,
            "market_age_s": round(self.market.age_seconds, 1) if self.market.taken_at else None,
            "wallets": {k: v.to_dict() for k, v in self.status.items()},
        }
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = FLEET_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        os.chmod(tmp, 0o600)
        tmp.replace(FLEET_STATE)

    def _sleep(self, event: threading.Event, seconds: float) -> None:
        event.wait(timeout=max(0.5, seconds))
        event.clear()
