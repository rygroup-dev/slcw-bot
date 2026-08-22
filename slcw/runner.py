"""Fleet daemon: one independent worker thread per wallet.

Replaces the oneshot systemd timer. Each worker sleeps on its own schedule, keeps
its own session, and trips its own circuit breaker — a failing wallet no longer
pauses the entire fleet, which is what the old global `data/paused` file did.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field

from . import inventory as inv_mod, ledger, leveling, market as market_mod, scheduler, tasks
from .api import GameApi
from .auth import AuthError, SessionManager
from .config import DATA, Config
from .market import MarketSnapshot
from . import clan as clan_mod
from . import discard as discard_mod
from .orchestrator import Orchestrator
from .transport import ApiError, Transport, TransportError
from .vault import SLEEP_HOURS_RANGE

logger = logging.getLogger(__name__)

FLEET_STATE = DATA / "fleet_state.json"

# Clan quest requirements move over hours, so re-reading them every cycle would
# spend two Firestore round trips per wallet to learn nothing new.
CLAN_CACHE_SECONDS = 900


@dataclass
class WalletStatus:
    wallet_id: str
    nickname: str = ""
    thread_alive: bool = False
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
    equipment: dict = field(default_factory=dict)
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
        # Per-wallet clan snapshot for the Telegram view, and the cache that
        # keeps clan reads off the hot path.
        self.last_clan: dict = {}
        self._clan_cache: dict = {}
        # The clan this fleet founded, and which wallets have already applied.
        # Shared across every worker so "found exactly once" holds fleet-wide.
        self.clan_registry = clan_mod.ClanRegistry()
        self._clan_doc: dict | None = None
        self._threads: dict = {}
        self._watchdog_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._wake_events: dict = {}

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        for wallet in self.vault.wallets():
            self.ensure_worker(wallet)
        with self._lock:
            if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog,
                    name="slcw-watchdog",
                    daemon=True,
                )
                self._watchdog_thread.start()

    def ensure_worker(self, wallet: dict) -> None:
        wallet_id = wallet["id"]
        with self._lock:
            existing = self._threads.get(wallet_id)
            if existing is not None and existing.is_alive():
                return
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
        # Whatever was destroyed since the last digest is reported now rather
        # than dying with the process.
        self.alerts.flush_discards(force=True)
        for event in self._wake_events.values():
            event.set()

    def _watchdog(self) -> None:
        """Keep wallet workers alive without coupling their circuit breakers.

        The watchdog supervises worker threads only. It does not run wallet
        cycles itself and never clears a wallet's paused/circuit-breaker state.
        """
        while not self._stop.wait(10):
            try:
                wallets = self.vault.wallets()

                for wallet in wallets:
                    wallet_id = wallet.get("id")
                    if not wallet_id:
                        continue

                    # Disabled wallets should not be restarted.
                    if not wallet.get("enabled", True):
                        continue

                    existing = self._threads.get(wallet_id)
                    if existing is not None and existing.is_alive():
                        continue

                    # Worker disappeared; restart only this wallet.
                    self.ensure_worker(wallet)

                self.persist()

                # Wallets only call in when they destroy something, so without
                # a tick here the last few stacks of a quiet night would sit
                # unreported until the next one was thrown away.
                self.alerts.flush_discards()

            except Exception:
                logger.exception("fleet watchdog error")

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
        """Contain unexpected errors so one wallet can never die silently."""
        status = self.status.get(wallet_id)
        if status:
            status.thread_alive = True

        try:
            while not self._stop.is_set():
                try:
                    self._worker_loop(wallet_id)
                    break
                except Exception as exc:
                    status = self.status.get(wallet_id)
                    if status:
                        self._register_error(status, f"worker crash: {exc}")
                    logger.exception("wallet worker %s crashed; retrying", wallet_id)
                    self._stop.wait(5)
        finally:
            status = self.status.get(wallet_id)
            if status:
                status.thread_alive = False

    def _worker_loop(self, wallet_id: str) -> None:
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

            # Wallets created before SLEEP_HOURS_RANGE narrowed to 3-4h are
            # still carrying whatever 6-9h value they were given at creation
            # — shrink it once, in place, rather than leaving old accounts on
            # the old schedule forever.
            if wallet.get("sleep_hours", 0) > SLEEP_HOURS_RANGE[1]:
                self.vault.update(wallet["id"],
                                  sleep_hours=round(random.uniform(*SLEEP_HOURS_RANGE), 2))

            state = api.get_player(session)
            self.refresh_market(api, session)

            # Refining, chest opening and equipment all need the inventory, so
            # it is fetched once and shared rather than read three times.
            try:
                inventory = inv_mod.parse_inventory(api.get_inventory(session))
                holdings = inventory.holdings()
            except (TransportError, ApiError):
                inventory, holdings = None, {}

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

            clan_context = self._clan_context(api, session, state, wallet["id"])
            # Keep the snapshot when the wallet is a member, and also when it
            # is not but the clan document was read — that is what lets the
            # Telegram view show seats before the first wallet is admitted.
            if (clan_context.get("membership") is not None
                    or clan_context.get("clan_info") is not None):
                self.last_clan[wallet["id"]] = clan_context

            orchestrator = Orchestrator(config=self.config, api=api, rng=rng)
            decision = orchestrator.decide_and_act(
                wallet, session, state, self.market, holdings, task_status,
                inventory, clan_context=clan_context)

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
            if inventory is not None:
                status.state["slots_used"] = inventory.used_slots
                status.state["slots_max"] = inventory.max_slots
                status.state["chests"] = sum(c.quantity for c in inventory.chests())
            status.state["professions"] = state.professions or {}
            status.state["attributes"] = state.attributes or {}
            status.state["xp_needed"] = leveling.xp_required(state.level)
            status.equipment = state.equipment or {}

            if decision.action in ("createClan", "applyClan", "resolveApplication",
                                   "generateClanQuest"):
                self._record_clan_outcome(wallet["id"], decision)
                # A clan action changes who this wallet is; drop the cached
                # snapshot so the next cycle re-reads it.
                self._clan_cache.pop(wallet["id"], None)
                # Founding, admitting and starting a quest all move numbers on
                # the clan document itself — seats taken, level, active quest —
                # so the fleet-wide copy has to go too.
                self._clan_doc = None

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

            # If the wallet is free again and something worthwhile is already
            # available, come back on a human reaction delay rather than the
            # idle poll — otherwise a one-minute battle is followed by a
            # quarter-hour wait while the energy bar sits full.
            action_ready = False
            if not fresh.is_busy:
                action_ready = bool(orchestrator.build_candidates(
                    fresh, self.market, holdings, task_status=task_status,
                    inventory=inventory, wallet_id=wallet["id"]))

            return scheduler.next_wake_seconds(
                self.config, wallet, fresh, rng=rng, action_ready=action_ready)

        except (AuthError, TransportError, ApiError) as exc:
            self.sessions.invalidate(wallet["id"])
            self._register_error(status, f"{type(exc).__name__}: {exc}")
            return scheduler.idle_delay(self.config, rng), "error backoff"
        except Exception as exc:  # never let one wallet kill its thread
            self._register_error(status, f"unexpected {type(exc).__name__}: {exc}")
            return scheduler.idle_delay(self.config, rng), "error backoff"
        finally:
            transport.close()

    def _clan_context(self, api, session, state, wallet_id: str) -> dict:
        """Membership and the active clan quest, cached per wallet.

        A wallet in no clan costs nothing here: the player document already says
        so. A member costs two Firestore reads, which is why the result is held
        for CLAN_CACHE_SECONDS rather than fetched every cycle — quest
        requirements move in hours, not seconds.
        """
        info = self._clan_info(api, session)
        base = {"membership": None, "quest": None,
                "registry": self.clan_registry,
                "fleet_uids": self._fleet_uids(), "applications": [],
                "clan_info": info,
                "seat_holders": self._seat_holders(info),
                "levels_by_uid": self._levels_by_uid()}
        if not self.config.clan_enabled:
            return {"membership": None, "quest": None}

        clan_id = str((state.raw or {}).get("clanId") or "")
        if clan_id:
            # Whichever wallet is in a clan, adopt that id once. This is what
            # makes founding survive a restart even if the create reply was lost.
            self.clan_registry.record_clan(clan_id, wallet_id)
        if not clan_id:
            self._clan_cache.pop(wallet_id, None)
            return base

        cached = self._clan_cache.get(wallet_id)
        if cached and time.time() - cached["fetched_at"] < CLAN_CACHE_SECONDS:
            return cached["context"]

        try:
            member_doc = api.get_clan_member(session, clan_id, session.local_id)
            membership = clan_mod.parse_membership(clan_id, member_doc)
            quest = None
            for doc in api.get_clan_quests(session, clan_id):
                parsed = clan_mod.parse_quest(doc, doc.get("questId", ""))
                if parsed is not None and not parsed.completed:
                    quest = parsed
                    break
            applications = []
            if membership.role in ("leader", "officer"):
                applications = api.get_clan_applications(session, clan_id)
            context = dict(base, membership=membership, quest=quest,
                           applications=applications)
        except (TransportError, ApiError):
            # A clan read failing must never stop the wallet playing the game.
            return base

        self._clan_cache[wallet_id] = {"fetched_at": time.time(), "context": context}
        return context

    def _clan_info(self, api, session) -> "clan_mod.ClanInfo | None":
        """The clan document, read once for the whole fleet and cached.

        Every wallet needs the same three numbers out of it — level, member
        count and seats — so reading it per wallet would multiply one Firestore
        document by thirty for nothing. A wallet that is not in the clan needs
        it too: that is exactly the wallet deciding whether to apply.
        """
        clan_id = self.clan_registry.clan_id
        if not self.config.clan_enabled or not clan_id:
            return None
        cached = getattr(self, "_clan_doc", None)
        if cached and time.time() - cached["fetched_at"] < CLAN_CACHE_SECONDS:
            return cached["info"]
        try:
            info = clan_mod.parse_clan(clan_id, api.get_clan(session, clan_id))
        except (TransportError, ApiError):
            # Never let a failed read look like a clan with no seats.
            return cached["info"] if cached else None
        self._clan_doc = {"fetched_at": time.time(), "info": info}
        return info

    def _wallet_levels(self) -> dict:
        """Wallet id -> level, for every wallet that has reported one."""
        return {wallet_id: int((status.state or {}).get("level", 0) or 0)
                for wallet_id, status in self.status.items()}

    def _levels_by_uid(self) -> dict:
        """The same levels keyed the way an application identifies its sender."""
        if not self.vault.is_unlocked:
            return {}
        levels = self._wallet_levels()
        out = {}
        for wallet in self.vault.wallets():
            key = wallet.get("public_key")
            if key:
                out[clan_mod.fleet_uid(key)] = levels.get(wallet.get("id"), 0)
        return out

    def _seat_holders(self, info) -> list | None:
        """Wallets entitled to one of the clan's seats, strongest first.

        A clan of level L holds 5L+5 members. With thirty wallets and a clan
        that starts at ten seats, most of the fleet must not apply at all — an
        application nobody can accept sits in the queue until it is cancelled by
        hand. The roster is simply the top wallets by level, so it widens on its
        own as clan quests raise the level, and a wallet added to the vault
        later competes for a seat on the same terms as the rest.
        """
        if info is None:
            return None
        if info.free_seats - self.clan_registry.pending_applications() <= 0:
            return []
        return clan_mod.seat_ranking(self._wallet_levels(), info.max_members)

    def _fleet_uids(self) -> set:
        """Player ids for every wallet in the vault, as the game derives them.

        The leader compares an application's `userId` against this set, so a
        stranger's request is never auto-accepted into a clan sized for one
        operator's fleet.
        """
        if not self.vault.is_unlocked:
            return set()
        return {clan_mod.fleet_uid(w.get("public_key", ""))
                for w in self.vault.wallets() if w.get("public_key")}

    def _record_clan_outcome(self, wallet_id: str, decision) -> None:
        """Persist what a clan action did, so it is never repeated blindly."""
        result = decision.result or {}
        payload = result.get("result") if isinstance(result.get("result"), dict) else result
        if decision.action == "createClan" and not decision.error:
            clan_id = str((payload or {}).get("clanId") or "")
            if clan_id:
                self.clan_registry.record_clan(clan_id, wallet_id)
            self.notifier.send(
                f"\U0001F6E1 <b>{wallet_id}</b> mendirikan clan "
                f"<b>{self.config.clan_name}</b> [{self.config.clan_tag}]"
                + (f"\n<code>{clan_id}</code>" if clan_id else ""))
        elif decision.action == "applyClan":
            if decision.error:
                return
            app_id = str((payload or {}).get("applicationId") or "")
            self.clan_registry.record_application(wallet_id, app_id)
        elif decision.action == "generateClanQuest" and not decision.error:
            quest = (payload or {}).get("quest") or payload or {}
            wanted = ", ".join(
                f"{r.get('required', 0)}x {r.get('itemName') or r.get('itemId')}"
                for r in (quest.get("requirements") or []))
            self.alerts.clan_quest(
                wanted, str(quest.get("questId") or quest.get("id") or ""),
                clan_mod.QUEST_CLAN_XP)
        elif decision.action == "resolveApplication" and not decision.error:
            # The applicant is now a member; drop its pending marker so a later
            # cycle does not think it is still waiting.
            for other, entry in list(
                    (self.clan_registry.data.get("applications") or {}).items()):
                if entry.get("application_id") == decision.params.get("applicationId"):
                    self.clan_registry.clear_application(other)

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

        if (decision.action == "deleteInventoryItem"
                and not decision.error and not decision.dry_run):
            # Written down before it is announced: the Telegram message can be
            # lost, muted or scrolled past, and this is the only record that the
            # item ever existed.
            item_id = decision.detail.get("item_id", "?")
            quantity = decision.detail.get("quantity", 0)
            discard_mod.record(status.wallet_id, item_id, quantity)
            self.alerts.discarded(status.wallet_id, item_id, quantity)

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
        """Atomically publish one fleet snapshot at a time.

        Every wallet worker calls this method.  Serialising the complete
        write/chmod/replace sequence prevents workers from racing over the
        shared ``fleet_state.tmp`` path and crashing with FileNotFoundError.
        """
        with self._persist_lock:
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
