"""Telegram control plane: inline keyboards, callback routing, vault unlock.

Every view edits the existing message rather than posting a new one, so the chat
stays a single live dashboard instead of a scroll of stale snapshots.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from slcw import keys, ledger, market as market_mod
from slcw.vault import VaultError

from . import ui

API_ROOT = "https://api.telegram.org/bot"

# The old /logs command shelled out to `journalctl -u slcw-bot`, a unit that has
# never existed, so it always returned nothing.
SERVICE_UNIT = "slcw-fleet"


class TelegramBot:
    def __init__(self, config, vault, fleet):
        self.config = config
        self.vault = vault
        self.fleet = fleet
        self.token = config.telegram_token
        self.allowed = {str(config.telegram_chat_id)}
        self.offset = 0
        self.running = False
        # Seed-phrase imports await an address choice; held in memory only.
        self.pending_import: list = []

    # --- transport -------------------------------------------------------
    def api(self, method: str, payload: dict | None = None) -> dict:
        data = urllib.parse.urlencode(payload or {}).encode()
        request = urllib.request.Request(
            f"{API_ROOT}{self.token}/{method}", data=data, method="POST")
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.loads(response.read())

    def send(self, chat_id, text: str, markup: str | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": "true"}
        if markup:
            payload["reply_markup"] = markup
        return self.api("sendMessage", payload)

    def edit(self, chat_id, message_id, text: str, markup: str | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                   "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if markup:
            payload["reply_markup"] = markup
        try:
            self.api("editMessageText", payload)
        except Exception:
            # "message is not modified" and expired-message errors are both benign.
            pass

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
        except Exception:
            pass

    def delete(self, chat_id, message_id) -> None:
        try:
            self.api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass

    # --- loop ------------------------------------------------------------
    def run(self) -> None:
        self.running = True
        try:
            self.api("deleteWebhook", {"drop_pending_updates": "false"})
        except Exception:
            pass
        self.set_commands()

        while self.running:
            try:
                data = self.api("getUpdates", {"timeout": 25, "offset": self.offset,
                                               "allowed_updates": json.dumps(
                                                   ["message", "callback_query"])})
                for update in data.get("result", []):
                    self.offset = update["update_id"] + 1
                    try:
                        self.dispatch(update)
                    except Exception as exc:
                        print(f"handler error: {type(exc).__name__}: {exc}", flush=True)
            except TimeoutError:
                # Expected: long polling returns nothing within the window.
                continue
            except Exception as exc:
                print(f"telegram poll error: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(5)

    def set_commands(self) -> None:
        try:
            self.api("setMyCommands", {"commands": json.dumps([
                {"command": "menu", "description": "Buka dashboard"},
                {"command": "status", "description": "Status fleet"},
                {"command": "unlock", "description": "Buka vault: /unlock <passphrase>"},
                {"command": "import", "description": "Import wallet: /import <kunci>"},
                {"command": "profit", "description": "Ledger keuntungan"},
                {"command": "market", "description": "Snapshot black market"},
            ])})
        except Exception:
            pass

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
            return self.send(chat_id, ui.render_status(self.fleet_state()), ui.main_menu())
        if command == "/profit":
            return self.send(chat_id, self.profit_text(), ui.main_menu())
        if command == "/market":
            return self.send(chat_id, ui.render_market(self.fleet.market), ui.main_menu())
        self.show_main(chat_id)

    def handle_unlock(self, chat_id, message: dict, text: str) -> None:
        # Delete the passphrase from the chat immediately, before anything else can
        # fail. The passphrase is only ever held in process memory.
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
        self.fleet.persist()
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
                self.vault.public_summary(),
                {k: v.to_dict() for k, v in self.fleet.status.items()}))

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
        self.fleet.persist()
        self.send(
            chat_id,
            f"✅ <b>{wallet['id']}</b> diimpor · {wallet['nickname']}\n"
            f"<code>{wallet['public_key']}</code>\n"
            f"Sumber: {candidate.source}\n\n"
            f"Worker sudah jalan. Kunci privat tersimpan terenkripsi dan tidak "
            f"akan pernah ditampilkan di sini.",
            ui.wallet_list(self.vault.public_summary(),
                           {k: v.to_dict() for k, v in self.fleet.status.items()}))

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
        handler(chat_id, message_id, callback_id, parts)

    # --- views -----------------------------------------------------------
    def fleet_state(self) -> dict:
        self.fleet.persist()
        from slcw.runner import FLEET_STATE
        if not FLEET_STATE.exists():
            return {}
        try:
            return json.loads(FLEET_STATE.read_text())
        except json.JSONDecodeError:
            return {}

    def show_main(self, chat_id) -> None:
        self.send(chat_id, ui.render_status(self.fleet_state()), ui.main_menu())

    def route_nav(self, chat_id, message_id, callback_id, parts) -> None:
        view = parts[0] if parts else "main"
        if view in ("main", "status"):
            self.edit(chat_id, message_id, ui.render_status(self.fleet_state()), ui.main_menu())
        elif view == "profit":
            self.edit(chat_id, message_id, self.profit_text(), ui.main_menu())
        elif view == "market":
            self.edit(chat_id, message_id, ui.render_market(self.fleet.market), ui.main_menu())
        elif view == "economy":
            self.edit(chat_id, message_id,
                      "<b>⚗️ Ekonomi</b>\n\n"
                      "Bahan mentah tidak punya bid sama sekali; yang laku hanya "
                      "barang olahan. Rantai profit menunjukkan di mana nilainya "
                      "muncul dan berapa ongkos tiap mata rantainya.",
                      ui.economy_menu())
        elif view == "chain":
            self.edit(chat_id, message_id,
                      ui.render_chain(self.fleet.market, self.config),
                      ui.economy_menu())
        elif view == "farming":
            self.edit(chat_id, message_id, self.farming_text(), ui.economy_menu())
        elif view == "refining":
            self.edit(chat_id, message_id, self.refining_text(), ui.economy_menu())
        elif view == "energy":
            self.edit(chat_id, message_id, ui.render_energy(self.fleet_state()),
                      ui.economy_menu())
        elif view == "map":
            self.edit(chat_id, message_id, ui.render_map(self.fleet_state()),
                      ui.economy_menu())
        elif view == "tasks":
            self.edit(chat_id, message_id,
                      ui.render_tasks(self.fleet.last_task_status), ui.main_menu())
        elif view == "combat":
            self.edit(chat_id, message_id, ui.render_combat(self.combat_memory()),
                      ui.main_menu())
        elif view == "control":
            paused = sum(1 for s in self.fleet.status.values() if s.paused)
            self.edit(chat_id, message_id,
                      self.control_text(paused),
                      ui.control_menu(paused, len(self.fleet.status), self.config.dry_run))
        elif view == "vault":
            self.edit(chat_id, message_id, self.vault_text(), ui.vault_menu(self.vault.is_unlocked))
        self.answer(callback_id)

    def control_text(self, paused: int) -> str:
        total = len(self.fleet.status)
        return (f"<b>⚙️ Kontrol</b>\n\n"
                f"Wallet: {total} · aktif {total - paused} · pause {paused}\n"
                f"Mode: {'🧪 dry-run' if self.config.dry_run else '🚀 live'}\n"
                f"Engine: {'aktif' if self.config.enabled else 'claim-only'}\n"
                f"Vault: {'terbuka' if self.vault.is_unlocked else 'terkunci'}")

    def vault_text(self) -> str:
        if self.vault.is_unlocked:
            return (f"<b>🔐 Vault terbuka</b>\n\n"
                    f"{len(self.vault.wallets())} wallet terdekripsi di memori.\n"
                    f"Private key tidak pernah ditulis ke disk dalam bentuk polos, "
                    f"dan tidak pernah dikirim lewat Telegram.")
        return ("<b>🔐 Vault terkunci</b>\n\n"
                "Kirim <code>/unlock passphrase-kamu</code>.\n"
                "Pesannya langsung dihapus dari chat setelah dibaca.")

    def combat_memory(self):
        from slcw.combat import CombatMemory
        return CombatMemory()

    def farming_text(self) -> str:
        """Render gathering economics for the first wallet with known state."""
        for status in self.fleet.status.values():
            state = status.state or {}
            if state:
                return ui.render_farming(
                    self.fleet.market,
                    level=state.get("level", 1),
                    grade=state.get("grade", 1),
                    gold=state.get("gold", 0),
                    energy=state.get("energy", 0),
                    config=self.config)
        return "Belum ada state wallet. Tunggu siklus pertama selesai."

    def refining_text(self) -> str:
        for status in self.fleet.status.values():
            state = status.state or {}
            if state:
                return ui.render_refining(
                    self.fleet.market,
                    level=state.get("level", 1),
                    grade=state.get("grade", 1),
                    gold=state.get("gold", 0),
                    holdings=status.holdings or {},
                    config=self.config)
        return "Belum ada state wallet. Tunggu siklus pertama selesai."

    def profit_text(self) -> str:
        totals, item_value = ledger.valued_totals(market=self.fleet.market)
        per_wallet = {w["id"]: ledger.totals(w["id"])
                      for w in (self.vault.wallets() if self.vault.is_unlocked else [])}
        return ui.render_profit(totals, item_value, per_wallet)

    # --- controls --------------------------------------------------------
    def route_control(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else ""
        note = ""

        if action == "resume_all":
            self.fleet.resume_all()
            note = "Semua wallet resume"
        elif action == "pause_all":
            self.fleet.pause_all("manual")
            note = "Semua wallet pause"
        elif action == "force":
            self.fleet.force_cycle()
            note = "Siklus dipaksa"
        elif action == "toggle_dry":
            import dataclasses
            self.config = dataclasses.replace(self.config, dry_run=not self.config.dry_run)
            self.fleet.config = self.config
            note = f"Dry-run {'ON' if self.config.dry_run else 'OFF'}"
        elif action == "logs":
            self.answer(callback_id, "Mengambil log")
            return self.edit(chat_id, message_id, self.logs_text(),
                             ui.control_menu(0, len(self.fleet.status), self.config.dry_run))
        elif action == "doctor":
            self.answer(callback_id, "Cek kesehatan")
            return self.edit(chat_id, message_id, self.doctor_text(),
                             ui.control_menu(0, len(self.fleet.status), self.config.dry_run))

        paused = sum(1 for s in self.fleet.status.values() if s.paused)
        self.edit(chat_id, message_id, self.control_text(paused),
                  ui.control_menu(paused, len(self.fleet.status), self.config.dry_run))
        self.answer(callback_id, note)

    def logs_text(self) -> str:
        try:
            output = subprocess.run(
                ["journalctl", "-u", SERVICE_UNIT, "-n", "25", "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=15).stdout
        except Exception as exc:
            return f"Gagal baca log: {exc}"
        if not output.strip():
            return f"Tidak ada log untuk unit <code>{SERVICE_UNIT}</code>."
        import html
        return f"<b>📜 {SERVICE_UNIT}</b>\n<pre>{html.escape(output[-3200:])}</pre>"

    def doctor_text(self) -> str:
        problems = self.config.validate()
        lines = ["<b>🩺 Doctor</b>", ""]
        lines.append(f"Python: <code>{sys.executable}</code>")
        lines.append(f"Vault: {'terbuka' if self.vault.is_unlocked else 'TERKUNCI'}")
        lines.append(f"Wallet: {len(self.fleet.status)}")
        alive = sum(1 for t in self.fleet._threads.values() if t.is_alive())
        lines.append(f"Worker hidup: {alive}/{len(self.fleet._threads)}")
        age = self.fleet.market.age_seconds
        lines.append(f"Market snapshot: "
                     f"{'belum ada' if age == float('inf') else f'{int(age) // 60}m lalu'}")
        proxied = sum(1 for w in (self.vault.wallets() if self.vault.is_unlocked else [])
                      if w.get("proxy"))
        lines.append(f"Proxy terpasang: {proxied} wallet")
        lines.append("")
        if problems:
            lines.append("<b>⚠️ Masalah config</b>")
            lines += [f"  • {p}" for p in problems]
        else:
            lines.append("✅ Config lengkap")
        return "\n".join(lines)

    # --- wallets ---------------------------------------------------------
    def route_wallet(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else "list"

        if not self.vault.is_unlocked:
            self.edit(chat_id, message_id, self.vault_text(), ui.vault_menu(False))
            return self.answer(callback_id, "Vault terkunci")

        if action == "list":
            wallets = self.vault.public_summary()
            status = {k: v.to_dict() for k, v in self.fleet.status.items()}
            text = (f"<b>👛 Wallets</b> · {len(wallets)} akun\n\n"
                    + "\n".join(f"<code>{w['public_key'][:16]}…</code> {w['id']} "
                                f"· {w['nickname']} · proxy {w['proxy']}" for w in wallets)
                    if wallets else "Belum ada wallet.")
            self.edit(chat_id, message_id, text, ui.wallet_list(wallets, status))
            return self.answer(callback_id)

        if action == "new":
            self.edit(chat_id, message_id,
                      "<b>➕ Tambah wallet</b>\n\n"
                      "<b>Buat baru:</b> keypair Solana dibuat lokal, langsung dienkripsi, "
                      "lalu onboarding in-game dijalankan otomatis di siklus pertama.\n\n"
                      "<b>Import:</b> pakai akun yang sudah ada.\n\n"
                      "Bot <b>tidak pernah</b> memindahkan dana — pendanaan kamu lakukan "
                      "sendiri kalau perlu.\n\nBerapa wallet baru?",
                      ui.new_wallet_menu())
            return self.answer(callback_id)

        if action == "importhelp":
            self.edit(chat_id, message_id, ui.IMPORT_HELP, ui.new_wallet_menu())
            return self.answer(callback_id)

        if action == "cancelimport":
            self.pending_import = []
            self.edit(chat_id, message_id, "Import dibatalkan.", ui.new_wallet_menu())
            return self.answer(callback_id, "Dibatalkan")

        if action == "pick":
            index = int(parts[1]) if len(parts) > 1 else -1
            candidates = getattr(self, "pending_import", [])
            if not 0 <= index < len(candidates):
                return self.answer(callback_id, "Pilihan kedaluwarsa — kirim /import lagi")
            candidate = candidates[index]
            self.pending_import = []
            self.delete(chat_id, message_id)
            self.finish_import(chat_id, candidate)
            return self.answer(callback_id, "Diimpor")

        if action == "create":
            count = int(parts[1]) if len(parts) > 1 else 1
            created = self.vault.create_wallets(count)
            for wallet in created:
                self.fleet.ensure_worker(wallet)
            listing = "\n".join(f"<code>{w['public_key']}</code>\n  {w['id']} · {w['nickname']}"
                                for w in created)
            self.edit(chat_id, message_id,
                      f"<b>✅ {len(created)} wallet dibuat</b>\n\n{listing}\n\n"
                      f"<i>Private key ada di vault terenkripsi dan tidak akan pernah "
                      f"ditampilkan di sini.</i>",
                      ui.wallet_list(self.vault.public_summary(),
                                     {k: v.to_dict() for k, v in self.fleet.status.items()}))
            return self.answer(callback_id, f"{len(created)} wallet dibuat")

        wallet_id = parts[1] if len(parts) > 1 else ""
        wallet = self.vault.get(wallet_id)
        status_obj = self.fleet.status.get(wallet_id)
        if wallet is None or status_obj is None:
            return self.answer(callback_id, "Wallet tidak ditemukan")
        status = status_obj.to_dict()

        if action == "pause":
            self.fleet.pause(wallet_id, "manual")
            self.answer(callback_id, "Dipause")
        elif action == "resume":
            self.fleet.resume(wallet_id)
            self.answer(callback_id, "Diresume")
        elif action == "force":
            self.fleet.force_cycle(wallet_id)
            self.answer(callback_id, "Siklus dipaksa")
        elif action == "why":
            self.edit(chat_id, message_id, ui.render_why(status),
                      ui.wallet_detail(wallet_id, status_obj.paused))
            return self.answer(callback_id)
        else:
            self.answer(callback_id)

        status = self.fleet.status[wallet_id].to_dict()
        self.edit(chat_id, message_id, ui.render_wallet(wallet, status),
                  ui.wallet_detail(wallet_id, self.fleet.status[wallet_id].paused))

    def route_vault(self, chat_id, message_id, callback_id, parts) -> None:
        action = parts[0] if parts else ""
        if action == "lock":
            self.vault.lock()
            self.fleet.pause_all("vault locked")
            self.answer(callback_id, "Vault dikunci")
        elif action == "howto":
            self.answer(callback_id, "Kirim /unlock <passphrase>")
        self.edit(chat_id, message_id, self.vault_text(), ui.vault_menu(self.vault.is_unlocked))
