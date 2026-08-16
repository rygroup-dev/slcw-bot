"""Telegram control plane.

Built around three latency rules, because a control surface that lags is a
control surface you stop trusting:

1. **Acknowledge first.** A callback is answered before any work begins, so the
   button's spinner clears in one round trip instead of waiting for the render.
2. **Never block the poll.** Updates are queued and handled by workers, so a slow
   view — logs, doctor — cannot delay the next button press.
3. **Never touch disk to read.** Views render from live in-memory fleet state;
   persistence happens on the worker threads' own schedule.
"""
from __future__ import annotations

import html
import json
import queue
import subprocess
import sys
import threading
import time

from slcw import keys, ledger
from slcw.vault import VaultError

from . import ui
from .client import TelegramClient

# The old /logs command shelled out to `journalctl -u slcw-bot`, a unit that has
# never existed, so it always returned nothing.
SERVICE_UNIT = "slcw-fleet"

WORKER_COUNT = 3


class TelegramBot:
    def __init__(self, config, vault, fleet):
        self.config = config
        self.vault = vault
        self.fleet = fleet
        self.client = TelegramClient(config.telegram_token)
        self.allowed = {str(config.telegram_chat_id)}
        self.offset = 0
        self.running = False
        # Seed-phrase imports await an address choice; held in memory only.
        self.pending_import: list = []
        self._queue: queue.Queue = queue.Queue()
        self._workers: list = []

    # --- transport -------------------------------------------------------
    def send(self, chat_id, text: str, markup: str | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": "true"}
        if markup:
            payload["reply_markup"] = markup
        return self.client.call_quietly("sendMessage", payload)

    def edit(self, chat_id, message_id, text: str, markup: str | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                   "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if markup:
            payload["reply_markup"] = markup
        # "message is not modified" is the common outcome of a refresh and is not
        # worth a retry or a log line.
        self.client.call_quietly("editMessageText", payload)

    def answer(self, callback_id: str, text: str = "") -> None:
        self.client.call_quietly("answerCallbackQuery",
                                 {"callback_query_id": callback_id, "text": text[:180]})

    def delete(self, chat_id, message_id) -> None:
        self.client.call_quietly("deleteMessage",
                                 {"chat_id": chat_id, "message_id": message_id})

    # --- loop ------------------------------------------------------------
    def run(self) -> None:
        self.running = True
        self.client.call_quietly("deleteWebhook", {"drop_pending_updates": "false"})
        self.set_commands()

        for index in range(WORKER_COUNT):
            worker = threading.Thread(target=self._worker, name=f"tg-worker-{index}",
                                      daemon=True)
            worker.start()
            self._workers.append(worker)

        while self.running:
            try:
                for update in self.client.poll(self.offset):
                    self.offset = update["update_id"] + 1
                    self._queue.put(update)
            except Exception as exc:
                print(f"telegram poll: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(3)

    def _worker(self) -> None:
        while self.running:
            try:
                update = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.dispatch(update)
            except Exception as exc:
                print(f"handler error: {type(exc).__name__}: {exc}", flush=True)
            finally:
                self._queue.task_done()

    def set_commands(self) -> None:
        self.client.call_quietly("setMyCommands", {"commands": json.dumps([
            {"command": "menu", "description": "Buka dashboard"},
            {"command": "status", "description": "Status fleet"},
            {"command": "unlock", "description": "Buka vault: /unlock <passphrase>"},
            {"command": "import", "description": "Import wallet: /import <kunci>"},
            {"command": "profit", "description": "Ledger keuntungan"},
            {"command": "market", "description": "Snapshot black market"},
        ])})

    def authorized(self, chat_id) -> bool:
        return str(chat_id) in self.allowed

    # --- routing ---------------------------------------------------------
    def dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or not self.authorized(chat_id):
            return
        self.on_message(chat_id, message)

    def on_message(self, chat_id, message: dict) -> None:
        text = (message.get("text") or "").strip()
        command = text.split()[0].lower() if text else "/menu"

        if command == "/unlock":
            return self.handle_unlock(chat_id, message, text)
        if command == "/import":
            return self.handle_import(chat_id, message, text)
        if command in ("/start", "/menu", "/help"):
            return self.show_main(chat_id)
        if command == "/status":
            return self.show_main(chat_id)
        if command == "/profit":
            return self.send(chat_id, self.profit_text(), ui.main_menu())
        if command == "/market":
            return self.send(chat_id, ui.render_market(self.fleet.market),
                             ui.main_menu())
        self.show_main(chat_id)

    def handle_unlock(self, chat_id, message: dict, text: str) -> None:
        # Delete the passphrase from the chat immediately, before anything else
        # can fail. It is only ever held in process memory.
        self.delete(chat_id, message.get("message_id"))

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return self.send(chat_id, "Format: <code>/unlock passphrase-kamu</code>")

        try:
            count = self.vault.unlock(parts[1])
        except VaultError as exc:
            return self.send(chat_id, f"❌ Gagal buka vault: {exc}")

        migrated = 0
        try:
            migrated = self.vault.import_legacy(parts[1])
        except Exception:
            pass

        self.fleet.start()
        note = f" (+{migrated} diimpor dari plaintext lama)" if migrated else ""
        self.send(chat_id, f"🔓 Vault terbuka — {count + migrated} wallet aktif{note}.",
                  ui.main_menu())

    def handle_import(self, chat_id, message: dict, text: str) -> None:
        """Import an existing account from a pasted secret.

        The message is deleted before the secret is even parsed, so a malformed
        key cannot leave credentials sitting in the chat history.
        """
        self.delete(chat_id, message.get("message_id"))

        if not self.vault.is_unlocked:
            return self.send(chat_id, "🔐 Buka vault dulu: <code>/unlock passphrase</code>")

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return self.send(chat_id, ui.IMPORT_HELP, ui.wallet_list(
                self.vault.public_summary(), self.status_map()))

        try:
            candidates = keys.parse_secret(parts[1])
        except keys.KeyImportError as exc:
            return self.send(chat_id, f"❌ Tidak bisa dibaca: {exc}")

        if len(candidates) == 1:
            return self.finish_import(chat_id, candidates[0])

        # A seed phrase maps to more than one account; the operator picks.
        self.pending_import = candidates
        listing = "\n".join(
            f"<b>{c.source}</b>\n<code>{c.public_key}</code>" for c in candidates)
        self.send(
            chat_id,
            f"<b>📥 Frasa ini cocok untuk {len(candidates)} alamat</b>\n\n{listing}\n\n"
            f"Pilih yang memang akunmu. Kalau salah, bot akan login ke akun kosong.",
            ui.import_choice_menu(candidates))

    def finish_import(self, chat_id, candidate) -> None:
        try:
            wallet = self.vault.import_wallet(candidate.private_key, candidate.public_key)
        except ValueError as exc:
            return self.send(chat_id, f"❌ Import gagal: {exc}")

        self.fleet.ensure_worker(wallet)
        self.send(
            chat_id,
            f"✅ <b>{wallet['id']}</b> diimpor · {wallet['nickname']}\n"
            f"<code>{wallet['public_key']}</code>\n"
            f"Sumber: {candidate.source}\n\n"
            f"Worker sudah jalan. Kunci privat tersimpan terenkripsi dan tidak "
            f"akan pernah ditampilkan di sini.",
            ui.wallet_list(self.vault.public_summary(), self.status_map()))

    def on_callback(self, callback: dict) -> None:
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        message_id = (callback.get("message") or {}).get("message_id")
        callback_id = callback.get("id", "")

        if chat_id is None or not self.authorized(chat_id):
            return self.answer(callback_id, "Tidak diizinkan")

        data = callback.get("data", "")
        namespace, _, rest = data.partition(":")
        parts = rest.split(":") if rest else []

        handler = {
            "nav": self.route_nav,
            "ctl": self.route_control,
            "wallet": self.route_wallet,
            "vault": self.route_vault,
        }.get(namespace)

        if handler is None:
            return self.answer(callback_id, "Tombol tidak dikenal")

        # Acknowledge before rendering. Telegram spins the button until this
        # lands, so answering first is what makes the UI feel immediate.
        self.answer(callback_id)
        handler(chat_id, message_id, callback_id, parts)

    # --- state -----------------------------------------------------------
    def status_map(self) -> dict:
        return {k: v.to_dict() for k, v in self.fleet.status.items()}

    def fleet_state(self) -> dict:
        """Render-ready fleet snapshot, built from memory.

        This used to persist to disk and read the file back on every navigation,
        which put a write in the path of a button press for no benefit.
        """
        # A view must never crash on missing data; a snapshot can legitimately be
        # absent before the first market fetch completes.
        market = getattr(self.fleet, "market", None)
        market_age = (round(market.age_seconds, 1)
                      if market is not None and market.taken_at else None)
        return {
            "updated_at": int(time.time()),
            "dry_run": self.config.dry_run,
            "enabled": self.config.enabled,
            "unlocked": self.vault.is_unlocked,
            "market_age_s": market_age,
            "wallets": self.status_map(),
        }

    # --- views -----------------------------------------------------------
    def show_main(self, chat_id) -> None:
        state = self.fleet_state()
        pages = ui.page_count(len(state.get("wallets") or {}))
        self.send(chat_id, ui.render_status(state), ui.status_menu(1, pages))

    def route_nav(self, chat_id, message_id, callback_id, parts) -> None:
        view = parts[0] if parts else "main"
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        if view in ("main", "status"):
            state = self.fleet_state()
            pages = ui.page_count(len(state.get("wallets") or {}))
            return self.edit(chat_id, message_id,
                             ui.render_status(state, page),
                             ui.status_menu(page, pages))

        render = {
            "profit": lambda: (self.profit_text(), ui.main_menu()),
            "market": lambda: (ui.render_market(self.fleet.market), ui.main_menu()),
            "economy": lambda: (ui.ECONOMY_INTRO, ui.economy_menu()),
            "chain": lambda: (ui.render_chain(self.fleet.market, self.config),
                              ui.economy_menu()),
            "farming": lambda: (self.farming_text(), ui.economy_menu()),
            "refining": lambda: (self.refining_text(), ui.economy_menu()),
            "energy": lambda: (ui.render_energy(self.fleet_state()), ui.economy_menu()),
            "map": lambda: (ui.render_map(self.fleet_state()), ui.economy_menu()),
            "combat": lambda: (ui.render_combat(self.combat_memory()), ui.main_menu()),
            "tasks": lambda: (ui.render_tasks(self.fleet.last_task_status),
                              ui.main_menu()),
            "inventory": lambda: (ui.render_inventory(self.fleet_state()),
                                  ui.main_menu()),
            "crafting": lambda: (self.crafting_text(), ui.main_menu()),
            "control": lambda: (self.control_text(), self.control_markup()),
            "vault": lambda: (self.vault_text(), ui.vault_menu(self.vault.is_unlocked)),
        }.get(view)

        if render is None:
            return
        text, markup = render()
        self.edit(chat_id, message_id, text, markup)

    def control_markup(self) -> str:
        paused = sum(1 for s in self.fleet.status.values() if s.paused)
        return ui.control_menu(paused, len(self.fleet.status), self.config.dry_run)

    def control_text(self) -> str:
        total = len(self.fleet.status)
        paused = sum(1 for s in self.fleet.status.values() if s.paused)
        return ui.render_control(
            total=total, paused=paused, dry_run=self.config.dry_run,
            enabled=self.config.enabled, unlocked=self.vault.is_unlocked,
            latency_ms=self.client.average_latency_ms,
            queue_depth=self._queue.qsize())

    def vault_text(self) -> str:
        return ui.render_vault(
            self.vault.is_unlocked,
            len(self.vault.wallets()) if self.vault.is_unlocked else 0)

    def combat_memory(self):
        from slcw.combat import CombatMemory
        return CombatMemory()

    def _first_state(self):
        for status in self.fleet.status.values():
            if status.state:
                return status
        return None

    def farming_text(self) -> str:
        status = self._first_state()
        if status is None:
            return ui.WAITING_FOR_CYCLE
        state = status.state
        return ui.render_farming(
            self.fleet.market, level=state.get("level", 1),
            grade=state.get("grade", 1), gold=state.get("gold", 0),
            energy=state.get("energy", 0), config=self.config)

    def refining_text(self) -> str:
        status = self._first_state()
        if status is None:
            return ui.WAITING_FOR_CYCLE
        state = status.state
        return ui.render_refining(
            self.fleet.market, level=state.get("level", 1),
            grade=state.get("grade", 1), gold=state.get("gold", 0),
            holdings=status.holdings or {}, config=self.config)

    def crafting_text(self) -> str:
        status = self._first_state()
        if status is None:
            return ui.WAITING_FOR_CYCLE
        state = status.state
        return ui.render_crafting(
            self.fleet_state(), holdings=status.holdings or {},
            gold=state.get("gold", 0), grade=state.get("grade", 1),
            professions=state.get("professions") or {},
            location=state.get("location", ""))

    def profit_text(self) -> str:
        totals, item_value = ledger.valued_totals(market=self.fleet.market)
        per_wallet = {w["id"]: ledger.totals(w["id"])
                      for w in (self.vault.wallets() if self.vault.is_unlocked else [])}
        return ui.render_profit(totals, item_value, per_wallet)

    # --- controls --------------------------------------------------------
    def route_control(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else ""

        if action == "logs":
            return self.edit(chat_id, message_id, self.logs_text(),
                             self.control_markup())
        if action == "doctor":
            return self.edit(chat_id, message_id, self.doctor_text(),
                             self.control_markup())

        if action == "resume_all":
            self.fleet.resume_all()
        elif action == "pause_all":
            self.fleet.pause_all("manual")
        elif action == "force":
            self.fleet.force_cycle()
        elif action == "toggle_dry":
            import dataclasses
            self.config = dataclasses.replace(self.config,
                                              dry_run=not self.config.dry_run)
            self.fleet.config = self.config

        self.edit(chat_id, message_id, self.control_text(), self.control_markup())

    def logs_text(self) -> str:
        try:
            output = subprocess.run(
                ["journalctl", "-u", SERVICE_UNIT, "-n", "25", "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=10).stdout
        except Exception as exc:
            return f"Gagal baca log: {exc}"
        if not output.strip():
            return f"Tidak ada log untuk unit <code>{SERVICE_UNIT}</code>."
        return f"<b>📜 {SERVICE_UNIT}</b>\n<pre>{html.escape(output[-3200:])}</pre>"

    def doctor_text(self) -> str:
        alive = sum(1 for t in self.fleet._threads.values() if t.is_alive())
        proxied = sum(1 for w in (self.vault.wallets() if self.vault.is_unlocked else [])
                      if w.get("proxy"))
        return ui.render_doctor(
            python=sys.executable,
            problems=self.config.validate(),
            unlocked=self.vault.is_unlocked,
            wallets=len(self.fleet.status),
            workers_alive=alive,
            workers_total=len(self.fleet._threads),
            market_age=self.fleet.market.age_seconds,
            proxied=proxied,
            latency_ms=self.client.average_latency_ms,
            api_calls=self.client.calls,
            queue_depth=self._queue.qsize(),
        )

    # --- wallets ---------------------------------------------------------
    def route_wallet(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else "list"

        if not self.vault.is_unlocked:
            return self.edit(chat_id, message_id, self.vault_text(),
                             ui.vault_menu(False))

        if action == "list":
            wallets = self.vault.public_summary()
            return self.edit(chat_id, message_id,
                             ui.render_wallet_list(wallets, self.status_map()),
                             ui.wallet_list(wallets, self.status_map()))

        if action == "new":
            return self.edit(chat_id, message_id, ui.NEW_WALLET_INTRO,
                             ui.new_wallet_menu())

        if action == "importhelp":
            return self.edit(chat_id, message_id, ui.IMPORT_HELP, ui.new_wallet_menu())

        if action == "cancelimport":
            self.pending_import = []
            return self.edit(chat_id, message_id, "Import dibatalkan.",
                             ui.new_wallet_menu())

        if action == "pick":
            index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
            candidates = self.pending_import
            if not 0 <= index < len(candidates):
                return self.answer(callback_id, "Pilihan kedaluwarsa — /import lagi")
            candidate = candidates[index]
            self.pending_import = []
            self.delete(chat_id, message_id)
            return self.finish_import(chat_id, candidate)

        if action == "create":
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            created = self.vault.create_wallets(count)
            for wallet in created:
                self.fleet.ensure_worker(wallet)
            return self.edit(chat_id, message_id, ui.render_created(created),
                             ui.wallet_list(self.vault.public_summary(),
                                            self.status_map()))

        wallet_id = parts[1] if len(parts) > 1 else ""
        wallet = self.vault.get(wallet_id)
        status_obj = self.fleet.status.get(wallet_id)
        if wallet is None or status_obj is None:
            return self.answer(callback_id, "Wallet tidak ditemukan")

        if action == "pause":
            self.fleet.pause(wallet_id, "manual")
        elif action == "resume":
            self.fleet.resume(wallet_id)
        elif action == "force":
            self.fleet.force_cycle(wallet_id)
        elif action == "why":
            return self.edit(chat_id, message_id,
                             ui.render_why(status_obj.to_dict()),
                             ui.wallet_detail(wallet_id, status_obj.paused))

        self.edit(chat_id, message_id,
                  ui.render_wallet(wallet, self.fleet.status[wallet_id].to_dict()),
                  ui.wallet_detail(wallet_id, self.fleet.status[wallet_id].paused))

    def route_vault(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else ""
        if action == "lock":
            self.vault.lock()
            self.fleet.pause_all("vault locked")
        self.edit(chat_id, message_id, self.vault_text(),
                  ui.vault_menu(self.vault.is_unlocked))
