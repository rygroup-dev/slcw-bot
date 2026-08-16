"""End-to-end exercise of the Telegram import flow, with no network involved."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solders.keypair import Keypair

from bot.telegram import TelegramBot
from slcw import vault as vault_mod
from slcw.config import Config
from slcw.vault import Vault

PHRASE = ("legal winner thank year wave sausage worth useful "
          "legal winner thank yellow")


class FakeFleet:
    def __init__(self, market=None):
        from slcw.market import MarketSnapshot
        self.status = {}
        # The real Fleet always holds a snapshot, empty until the first fetch.
        self.market = market if market is not None else MarketSnapshot()
        self.last_task_status = None
        self.workers = []
        self._threads = {}

    def ensure_worker(self, wallet):
        self.workers.append(wallet["id"])

    def persist(self):
        pass

    def start(self):
        pass


class RecordingBot(TelegramBot):
    """Captures outbound calls instead of talking to Telegram."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.deleted = []
        self.edited = []
        self.answered = []

    def send(self, chat_id, text, markup=None):
        self.sent.append(text)
        return {}

    def edit(self, chat_id, message_id, text, markup=None):
        self.edited.append(text)

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)

    def answer(self, callback_id, text=""):
        self.answered.append(text)


class ImportFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = patch.object(vault_mod, "VAULT_PATH",
                                  Path(self.tmp.name) / "wallets.enc")
        self._path.start()
        self._n = patch.object(vault_mod, "SCRYPT_N", 2 ** 12)
        self._n.start()

        self.vault = Vault()
        self.vault.unlock("passphrase for tests")
        self.fleet = FakeFleet()
        self.bot = RecordingBot(
            Config(telegram_token="t", telegram_chat_id="42"), self.vault, self.fleet)
        self.keypair = Keypair()

    def tearDown(self):
        self._n.stop()
        self._path.stop()
        self.tmp.cleanup()

    def _import(self, secret):
        self.bot.handle_import(42, {"message_id": 7}, f"/import {secret}")

    def test_secret_message_is_deleted_before_anything_else(self):
        self._import(str(self.keypair))
        self.assertEqual(self.bot.deleted, [7])

    def test_message_deleted_even_when_the_key_is_garbage(self):
        self._import("not-a-key")
        self.assertEqual(self.bot.deleted, [7],
                         "a bad paste must not be left sitting in the chat")
        self.assertIn("Tidak bisa dibaca", self.bot.sent[-1])

    def test_base58_import_adds_the_wallet_and_starts_a_worker(self):
        self._import(str(self.keypair))
        self.assertEqual(len(self.vault.wallets()), 1)
        self.assertEqual(self.vault.wallets()[0]["public_key"],
                         str(self.keypair.pubkey()))
        self.assertEqual(len(self.fleet.workers), 1)

    def test_json_array_import(self):
        self._import(json.dumps(list(bytes(self.keypair))))
        self.assertEqual(self.vault.wallets()[0]["public_key"],
                         str(self.keypair.pubkey()))

    def test_confirmation_never_echoes_the_private_key(self):
        self._import(str(self.keypair))
        for message in self.bot.sent:
            self.assertNotIn(str(self.keypair), message)
        self.assertIn(str(self.keypair.pubkey()), self.bot.sent[-1])

    def test_seed_phrase_asks_which_address_before_importing(self):
        self._import(PHRASE)
        self.assertEqual(len(self.vault.wallets()), 0,
                         "an ambiguous phrase must not import silently")
        self.assertEqual(len(self.bot.pending_import), 2)
        self.assertIn("2 alamat", self.bot.sent[-1])

    def test_picking_an_address_completes_the_seed_phrase_import(self):
        self._import(PHRASE)
        expected = self.bot.pending_import[0].public_key
        self.bot.route_wallet(42, 9, "cb", ["pick", "0"])
        self.assertEqual(len(self.vault.wallets()), 1)
        self.assertEqual(self.vault.wallets()[0]["public_key"], expected)
        self.assertEqual(self.bot.pending_import, [])

    def test_picking_the_other_address_imports_a_different_account(self):
        self._import(PHRASE)
        expected = self.bot.pending_import[1].public_key
        self.bot.route_wallet(42, 9, "cb", ["pick", "1"])
        self.assertEqual(self.vault.wallets()[0]["public_key"], expected)

    def test_stale_pick_is_refused(self):
        self.bot.route_wallet(42, 9, "cb", ["pick", "0"])
        self.assertEqual(len(self.vault.wallets()), 0)
        self.assertIn("kedaluwarsa", self.bot.answered[-1])

    def test_cancel_clears_the_pending_import(self):
        self._import(PHRASE)
        self.bot.route_wallet(42, 9, "cb", ["cancelimport"])
        self.assertEqual(self.bot.pending_import, [])
        self.assertEqual(len(self.vault.wallets()), 0)

    def test_duplicate_import_is_reported_not_silently_dropped(self):
        self._import(str(self.keypair))
        self._import(str(self.keypair))
        self.assertEqual(len(self.vault.wallets()), 1)
        self.assertIn("Import gagal", self.bot.sent[-1])

    def test_import_requires_an_unlocked_vault(self):
        self.vault.lock()
        self._import(str(self.keypair))
        self.assertIn("Buka vault dulu", self.bot.sent[-1])

    def test_bare_command_shows_help(self):
        self.bot.handle_import(42, {"message_id": 7}, "/import")
        self.assertIn("Format yang diterima", self.bot.sent[-1])



class OrderingBot(RecordingBot):
    """Records the sequence of outbound calls, not just their contents."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sequence = []

    def answer(self, callback_id, text=""):
        self.sequence.append("answer")
        super().answer(callback_id, text)

    def edit(self, chat_id, message_id, text, markup=None):
        self.sequence.append("edit")
        super().edit(chat_id, message_id, text, markup)

    def send(self, chat_id, text, markup=None):
        self.sequence.append("send")
        return super().send(chat_id, text, markup)


class LatencyBehaviourTests(unittest.TestCase):
    """Telegram spins the button until answerCallbackQuery lands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = patch.object(vault_mod, "VAULT_PATH",
                                  Path(self.tmp.name) / "wallets.enc")
        self._path.start()
        self._n = patch.object(vault_mod, "SCRYPT_N", 2 ** 12)
        self._n.start()
        self.vault = Vault()
        self.vault.unlock("passphrase for tests")
        self.bot = OrderingBot(
            Config(telegram_token="t", telegram_chat_id="42"), self.vault, FakeFleet())

    def tearDown(self):
        self._n.stop()
        self._path.stop()
        self.tmp.cleanup()

    def _callback(self, data):
        return {"id": "cb-1", "data": data,
                "message": {"message_id": 5, "chat": {"id": 42}}}

    def test_callback_is_acknowledged_before_rendering(self):
        self.bot.on_callback(self._callback("nav:status"))
        self.assertEqual(self.bot.sequence[0], "answer",
                         "the spinner must clear before the render round trip")

    def test_every_main_menu_button_acknowledges_first(self):
        import json as _json
        from bot import ui
        for row in _json.loads(ui.main_menu())["inline_keyboard"]:
            for button in row:
                self.bot.sequence = []
                self.bot.on_callback(self._callback(button["callback_data"]))
                self.assertTrue(self.bot.sequence, button["callback_data"])
                self.assertEqual(self.bot.sequence[0], "answer",
                                 f"{button['callback_data']} rendered before ack")

    def test_unauthorised_chat_is_rejected_without_rendering(self):
        callback = self._callback("nav:status")
        callback["message"]["chat"]["id"] = 999
        self.bot.on_callback(callback)
        self.assertEqual(self.bot.sequence, ["answer"])

    def test_unknown_namespace_only_acknowledges(self):
        self.bot.on_callback(self._callback("bogus:thing"))
        self.assertEqual(self.bot.sequence, ["answer"])

    def test_reading_fleet_state_writes_nothing_to_disk(self):
        before = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.bot.fleet_state()
        self.bot.fleet_state()
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), before)

if __name__ == "__main__":
    unittest.main()
