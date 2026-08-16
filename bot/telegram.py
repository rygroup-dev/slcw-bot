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

    def send_document(self, chat_id, filename: str, content: str,
                      caption: str = "") -> int | None:
        """Upload text as a file. Returns the message id, for later deletion."""
        payload = self.client.upload(
            "sendDocument",
            {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"},
            filename, content)
        return ((payload or {}).get("result") or {}).get("message_id")

    def delete_after(self, chat_id, message_id, seconds: int) -> None:
        """Remove a message once the operator has had time to save it.

        Runs on its own timer thread so the handler returns immediately and the
        worker stays free for the next button press.
        """
        if not message_id:
            return
        timer = threading.Timer(seconds, self.delete, args=(chat_id, message_id))
        timer.daemon = True
        timer.start()

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
        if command == "/send":
            return self.handle_send_command(chat_id, text)
        if command == "/sweep":
            return self.handle_sweep_command(chat_id)
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
            "profile": lambda: (ui.render_profile(self.fleet_state(), self.config.build),
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

        if action == "tools":
            return self.edit(chat_id, message_id, ui.WALLET_TOOLS_INTRO,
                             ui.wallet_tools_menu())

        # --- export -------------------------------------------------------
        if action == "exportask":
            return self.edit(chat_id, message_id,
                             ui.render_export_warning(len(self.vault.wallets())),
                             ui.export_confirm_menu())

        if action == "exportgo":
            return self.do_export(chat_id, message_id)

        # --- send SOL -----------------------------------------------------
        if action == "sendask":
            return self.edit(chat_id, message_id,
                             ui.render_send_ask(self.funded_wallets()),
                             ui.send_amount_menu())

        if action == "sendamt":
            amount = float(parts[1]) if len(parts) > 1 else 0.0
            funded = self.funded_wallets()
            if not funded:
                return self.edit(chat_id, message_id, ui.render_send_ask([]),
                                 ui.wallet_tools_menu())
            return self.edit(chat_id, message_id,
                             f"<b>💸 {amount:g} SOL per wallet</b>\n\n"
                             f"Pilih wallet yang membayar:",
                             ui.send_source_menu(funded, amount))

        if action == "sendsrc":
            amount = float(parts[1]) if len(parts) > 1 else 0.0
            source_id = parts[2] if len(parts) > 2 else ""
            return self.preview_send(chat_id, message_id, amount, source_id)

        if action == "sendgo":
            amount = float(parts[1]) if len(parts) > 1 else 0.0
            source_id = parts[2] if len(parts) > 2 else ""
            return self.do_send(chat_id, message_id, amount, source_id)

        if action == "manualhelp":
            return self.edit(chat_id, message_id, ui.MANUAL_AMOUNT_HELP,
                             ui.wallet_tools_menu())

        # --- primary wallet ------------------------------------------------
        if action == "primaryask":
            summary = self.vault.public_summary()
            return self.edit(chat_id, message_id, ui.render_primary(summary),
                             ui.primary_picker_menu(summary))

        if action == "primaryset":
            target = parts[1] if len(parts) > 1 else ""
            try:
                self.vault.set_primary(target)
            except KeyError:
                return self.answer(callback_id, "Wallet tidak ditemukan")
            summary = self.vault.public_summary()
            return self.edit(chat_id, message_id, ui.render_primary(summary),
                             ui.primary_picker_menu(summary))

        # --- sweep back to primary -----------------------------------------
        if action == "sweepask":
            return self.preview_sweep(chat_id, message_id)

        if action == "sweepgo":
            return self.do_sweep(chat_id, message_id,
                                 parts[1] if len(parts) > 1 else "")

        # --- wallet to wallet ----------------------------------------------
        if action == "p2pask":
            funded = self.funded_wallets()
            if not funded:
                return self.edit(chat_id, message_id, ui.render_send_ask([]),
                                 ui.wallet_tools_menu())
            return self.edit(chat_id, message_id,
                             "<b>🔁 Antar wallet</b>\n\nPilih wallet pengirim:",
                             ui.p2p_source_menu(funded))

        if action == "p2psrc":
            source_id = parts[1] if len(parts) > 1 else ""
            return self.edit(chat_id, message_id,
                             f"<b>🔁 Dari {source_id}</b>\n\nPilih tujuan:",
                             ui.p2p_dest_menu(source_id, self.vault.public_summary()))

        if action == "p2pdst":
            source_id = parts[1] if len(parts) > 1 else ""
            destination_id = parts[2] if len(parts) > 2 else ""
            return self.edit(
                chat_id, message_id,
                f"<b>🔁 {source_id} → {destination_id}</b>\n\nPilih jumlah:",
                ui.amount_menu(f"wallet:p2pamt:{source_id}:{destination_id}"))

        if action == "p2pamt":
            source_id = parts[1] if len(parts) > 1 else ""
            destination_id = parts[2] if len(parts) > 2 else ""
            amount = float(parts[3]) if len(parts) > 3 else 0.0
            return self.preview_p2p(chat_id, message_id, source_id,
                                    destination_id, amount)

        if action == "p2pgo":
            amount = float(parts[1]) if len(parts) > 1 else 0.0
            source_id = parts[2] if len(parts) > 2 else ""
            destination_id = parts[3] if len(parts) > 3 else ""
            return self.do_p2p(chat_id, message_id, amount, source_id, destination_id)

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

    # --- wallet tools: keys and funds ------------------------------------
    EXPORT_DELETE_SECONDS = 60

    def do_export(self, chat_id, message_id) -> None:
        """Send every private key as a JSON document, then delete it."""
        from slcw import backup

        wallets = self.vault.wallets()
        if not wallets:
            return self.edit(chat_id, message_id, "Belum ada wallet untuk diekspor.",
                             ui.wallet_tools_menu())

        payload = backup.export_payload(wallets, note="exported from Telegram")

        # A backup nobody verified is a backup nobody has: re-derive every key
        # and confirm it matches the public key stored beside it.
        problems = backup.verify_payload(payload)
        if problems:
            return self.edit(
                chat_id, message_id,
                "❌ <b>Export dibatalkan</b>\n\nCadangan gagal diverifikasi:\n"
                + "\n".join(f"  • {html.escape(p)}" for p in problems[:5]),
                ui.wallet_tools_menu())

        import json as _json
        filename = backup.export_filename(len(wallets))
        try:
            sent_id = self.send_document(
                chat_id, filename, _json.dumps(payload, indent=2),
                caption=f"🔐 {len(wallets)} private key — dihapus dalam "
                        f"{self.EXPORT_DELETE_SECONDS} detik")
        except Exception as exc:
            return self.edit(chat_id, message_id,
                             f"❌ Gagal mengirim file: {html.escape(str(exc)[:150])}",
                             ui.wallet_tools_menu())

        self.delete_after(chat_id, sent_id, self.EXPORT_DELETE_SECONDS)
        self.edit(chat_id, message_id,
                  ui.render_export_sent(len(wallets), self.EXPORT_DELETE_SECONDS),
                  ui.wallet_tools_menu())

    def handle_send_command(self, chat_id, text: str) -> None:
        """`/send <amount> [source] [destination]` — typed rather than tapped.

        Everything the buttons can do is reachable here too, because a preset
        list can never cover the amount someone actually wants.
        """
        from slcw import solana

        if not self.vault.is_unlocked:
            return self.send(chat_id, "🔐 Buka vault dulu: <code>/unlock passphrase</code>")

        parts = text.split()
        if len(parts) < 2:
            return self.send(chat_id, ui.MANUAL_AMOUNT_HELP, ui.wallet_tools_menu())

        try:
            amount = solana.parse_amount(parts[1])
        except ValueError as exc:
            return self.send(chat_id, f"❌ {html.escape(str(exc))}")

        primary = self.vault.primary()
        source_id = parts[2] if len(parts) > 2 else (primary or {}).get("id", "")
        destination_id = parts[3] if len(parts) > 3 else ""

        if not self.vault.get(source_id):
            return self.send(chat_id, f"❌ Wallet sumber <code>{html.escape(source_id)}</code> "
                                      f"tidak ditemukan.")

        if destination_id:
            if not self.vault.get(destination_id):
                return self.send(chat_id, f"❌ Wallet tujuan "
                                          f"<code>{html.escape(destination_id)}</code> "
                                          f"tidak ditemukan.")
            return self.preview_p2p(chat_id, None, source_id, destination_id, amount)

        message = self.send(chat_id, "Menghitung…")
        message_id = ((message or {}).get("result") or {}).get("message_id")
        self.preview_send(chat_id, message_id, amount, source_id)

    def handle_sweep_command(self, chat_id) -> None:
        if not self.vault.is_unlocked:
            return self.send(chat_id, "🔐 Buka vault dulu: <code>/unlock passphrase</code>")
        message = self.send(chat_id, "Menghitung…")
        message_id = ((message or {}).get("result") or {}).get("message_id")
        self.preview_sweep(chat_id, message_id)

    # --- sweep: every wallet back to primary -----------------------------
    def preview_sweep(self, chat_id, message_id) -> None:
        from slcw import solana

        primary = self.vault.primary()
        if primary is None:
            return self.edit(chat_id, message_id, "Belum ada wallet.",
                             ui.wallet_tools_menu())

        wallets = self.vault.wallets()
        client = self.solana_client()
        try:
            balances = client.balances([w["public_key"] for w in wallets])
        except Exception as exc:
            return self.edit(chat_id, message_id,
                             f"❌ RPC gagal: {html.escape(str(exc)[:150])}",
                             ui.wallet_tools_menu())
        finally:
            client.close()

        plan = solana.plan_sweep(wallets, primary, balances)
        markup = (ui.sweep_confirm_menu(primary["id"]) if plan.entries
                  else ui.wallet_tools_menu())
        self.edit(chat_id, message_id, ui.render_sweep_plan(plan), markup)

    def do_sweep(self, chat_id, message_id, destination_id: str) -> None:
        from slcw import solana

        destination = self.vault.get(destination_id)
        if destination is None:
            return self.edit(chat_id, message_id, "Wallet tujuan tidak ditemukan.",
                             ui.wallet_tools_menu())

        wallets = self.vault.wallets()
        client = self.solana_client()
        try:
            # Balances are re-read rather than trusted from the preview; they
            # can move between the render and the press.
            balances = client.balances([w["public_key"] for w in wallets])
            plan = solana.plan_sweep(wallets, destination, balances)
            if not plan.entries:
                return self.edit(chat_id, message_id, ui.render_sweep_plan(plan),
                                 ui.wallet_tools_menu())
            self.edit(chat_id, message_id,
                      f"↩️ Menarik dari {plan.count} wallet…", None)
            results = solana.execute_sweep(client, plan)
        except Exception as exc:
            return self.edit(chat_id, message_id,
                             f"❌ Gagal: {html.escape(str(exc)[:200])}",
                             ui.wallet_tools_menu())
        finally:
            client.close()

        self.edit(chat_id, message_id, ui.render_send_results(results),
                  ui.wallet_tools_menu())

    # --- wallet to wallet -------------------------------------------------
    def preview_p2p(self, chat_id, message_id, source_id: str,
                    destination_id: str, amount: float) -> None:
        from slcw import solana

        source = self.vault.get(source_id)
        destination = self.vault.get(destination_id)
        if source is None or destination is None:
            return self.send(chat_id, "Wallet tidak ditemukan.")

        client = self.solana_client()
        try:
            balance = client.balance(source["public_key"])
        except Exception as exc:
            return self.send(chat_id, f"❌ RPC gagal: {html.escape(str(exc)[:150])}")
        finally:
            client.close()

        needed = (solana.sol_to_lamports(amount) + solana.BASE_FEE_LAMPORTS
                  + solana.RENT_EXEMPT_LAMPORTS)
        affordable = balance >= needed
        text = ui.render_p2p_plan(source, destination, amount,
                                  solana.lamports_to_sol(balance), affordable)
        markup = (ui.keyboard([
            [("⚠️ Ya, kirim", f"wallet:p2pgo:{amount:g}:{source_id}:{destination_id}")],
            [("✖️ Batal", "wallet:tools")]]) if affordable else ui.wallet_tools_menu())

        if message_id:
            self.edit(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def do_p2p(self, chat_id, message_id, amount: float, source_id: str,
               destination_id: str) -> None:
        from slcw import solana
        from solders.keypair import Keypair

        source = self.vault.get(source_id)
        destination = self.vault.get(destination_id)
        if source is None or destination is None:
            return self.edit(chat_id, message_id, "Wallet tidak ditemukan.",
                             ui.wallet_tools_menu())

        client = self.solana_client()
        try:
            balance = client.balance(source["public_key"])
            lamports = solana.sol_to_lamports(amount)
            if balance < lamports + solana.BASE_FEE_LAMPORTS + solana.RENT_EXEMPT_LAMPORTS:
                return self.edit(chat_id, message_id,
                                 "❌ Saldo tidak cukup lagi — mungkin berubah "
                                 "sejak konfirmasi.", ui.wallet_tools_menu())
            signature = client.send_sol(
                Keypair.from_base58_string(source["private_key"]),
                destination["public_key"], lamports)
        except Exception as exc:
            return self.edit(chat_id, message_id,
                             f"❌ Gagal: {html.escape(str(exc)[:200])}",
                             ui.wallet_tools_menu())
        finally:
            client.close()

        self.edit(chat_id, message_id,
                  f"✅ <b>{amount:g} SOL</b> terkirim\n"
                  f"{source_id} → {destination_id}\n\n"
                  f"<code>{html.escape(signature)}</code>",
                  ui.wallet_tools_menu())

    def solana_client(self):
        from slcw.solana import SolanaClient
        return SolanaClient(self.config.solana_rpc)

    def funded_wallets(self) -> list:
        """Wallets holding enough SOL to be worth offering as a source."""
        from slcw.solana import RENT_EXEMPT_LAMPORTS, lamports_to_sol

        wallets = self.vault.wallets()
        if not wallets:
            return []
        client = self.solana_client()
        try:
            balances = client.balances([w["public_key"] for w in wallets])
        except Exception:
            return []
        finally:
            client.close()

        funded = []
        for wallet in wallets:
            lamports = balances.get(wallet["public_key"], 0)
            if lamports > RENT_EXEMPT_LAMPORTS:
                funded.append({**wallet, "balance_lamports": lamports,
                               "balance_sol": lamports_to_sol(lamports)})
        return sorted(funded, key=lambda w: -w["balance_lamports"])

    def build_plan(self, amount: float, source_id: str):
        from slcw import solana

        source = self.vault.get(source_id)
        if source is None:
            return None, None
        client = self.solana_client()
        try:
            balance = client.balance(source["public_key"])
        finally:
            client.close()
        plan = solana.plan_distribution(source, self.vault.wallets(), amount, balance)
        return source, plan

    def preview_send(self, chat_id, message_id, amount: float, source_id: str) -> None:
        source, plan = self.build_plan(amount, source_id)
        if plan is None:
            return self.edit(chat_id, message_id, "Wallet sumber tidak ditemukan.",
                             ui.wallet_tools_menu())

        markup = (ui.send_confirm_menu(amount, source_id) if plan.affordable
                  else ui.wallet_tools_menu())
        self.edit(chat_id, message_id,
                  ui.render_send_plan(plan, source.get("nickname", "")), markup)

    def do_send(self, chat_id, message_id, amount: float, source_id: str) -> None:
        """Execute the fan-out. The plan is rebuilt and re-checked first.

        Balances can change between the preview and the press, so the numbers
        are taken again rather than trusted from the button.
        """
        from slcw import solana

        source, plan = self.build_plan(amount, source_id)
        if plan is None:
            return self.edit(chat_id, message_id, "Wallet sumber tidak ditemukan.",
                             ui.wallet_tools_menu())
        if not plan.affordable:
            return self.edit(chat_id, message_id,
                             ui.render_send_plan(plan, source.get("nickname", "")),
                             ui.wallet_tools_menu())

        self.edit(chat_id, message_id,
                  f"💸 Mengirim ke {plan.count} wallet…", None)

        client = self.solana_client()
        try:
            results = solana.execute_distribution(client, source, plan)
        except Exception as exc:
            return self.edit(chat_id, message_id,
                             f"❌ Pengiriman gagal: {html.escape(str(exc)[:200])}",
                             ui.wallet_tools_menu())
        finally:
            client.close()

        self.edit(chat_id, message_id, ui.render_send_results(results),
                  ui.wallet_tools_menu())

    def route_vault(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else ""
        if action == "lock":
            self.vault.lock()
            self.fleet.pause_all("vault locked")
        self.edit(chat_id, message_id, self.vault_text(),
                  ui.vault_menu(self.vault.is_unlocked))
