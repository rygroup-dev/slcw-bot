import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slcw import setup_wizard as wiz


class PassphraseTests(unittest.TestCase):
    def test_generated_passphrase_has_words_and_digits(self):
        phrase = wiz.generate_passphrase()
        self.assertEqual(len(phrase.split("-")), 6)
        self.assertTrue(phrase.split("-")[-1].isdigit())

    def test_passphrases_are_not_repeated(self):
        self.assertGreater(len({wiz.generate_passphrase() for _ in range(200)}), 190)

    def test_passphrase_is_long_enough_to_matter(self):
        self.assertGreaterEqual(len(wiz.generate_passphrase()), 20)


class EnvTests(unittest.TestCase):
    SAMPLE = """
# a comment
TELEGRAM_BOT_TOKEN=abc123
TELEGRAM_CHAT_ID = 42

SLCW_DRY_RUN=false
malformed line
"""

    def test_parses_keys_and_ignores_noise(self):
        values = wiz.parse_env(self.SAMPLE)
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "abc123")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "42")
        self.assertNotIn("malformed line", values)

    def test_merge_prefers_updates(self):
        merged = wiz.merge_env({"A": "old", "B": "keep"}, {"A": "new"})
        self.assertEqual(merged, {"A": "new", "B": "keep"})

    def test_merge_never_blanks_an_existing_value(self):
        merged = wiz.merge_env({"A": "keep"}, {"A": ""})
        self.assertEqual(merged["A"], "keep")

    def test_missing_credentials_detected(self):
        self.assertEqual(
            wiz.missing_credentials({"TELEGRAM_BOT_TOKEN": "x"}),
            ["TELEGRAM_CHAT_ID", "SLCW_FIREBASE_API_KEY"])

    def test_complete_credentials_report_nothing_missing(self):
        self.assertEqual(wiz.missing_credentials({
            "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1",
            "SLCW_FIREBASE_API_KEY": "k"}), [])

    def test_rendered_env_round_trips(self):
        values = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42",
                  "SLCW_FIREBASE_API_KEY": "key"}
        reparsed = wiz.parse_env(wiz.render_env(values))
        for key, value in values.items():
            self.assertEqual(reparsed[key], value)

    def test_rendered_env_preserves_customised_settings(self):
        rendered = wiz.render_env({"SLCW_DRY_RUN": "true", "SLCW_GOLD_RESERVE": "9999"})
        reparsed = wiz.parse_env(rendered)
        self.assertEqual(reparsed["SLCW_DRY_RUN"], "true")
        self.assertEqual(reparsed["SLCW_GOLD_RESERVE"], "9999")

    def test_rendered_env_only_contains_keys_config_reads(self):
        from slcw.config import Config

        rendered_keys = set(wiz.parse_env(wiz.render_env({})))
        source = Path(Config.__module__.replace(".", "/") + ".py")
        text = (Path(__file__).resolve().parent.parent / source).read_text()
        for key in rendered_keys:
            self.assertIn(key, text, f"{key} is written to .env but never read")


class WritePrivateTests(unittest.TestCase):
    def test_written_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.env"
            wiz.write_private(path, "SLCW_VAULT_PASSPHRASE=hunter2\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("hunter2", path.read_text())

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.env"
            wiz.write_private(path, "a=1\n")
            wiz.write_private(path, "a=2\n")
            self.assertEqual(path.read_text(), "a=2\n")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["secret.env"])


class UnlockModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_path = Path(self.tmp.name) / ".vault-key"
        self._patch = patch.object(wiz, "VAULT_KEY_PATH", self.key_path)
        self._patch.start()
        # The wizard is chatty by design; keep the test output readable.
        self._quiet = [patch.object(wiz, name) for name in ("heading", "ok", "note")]
        for quiet in self._quiet:
            quiet.start()

    def tearDown(self):
        for quiet in self._quiet:
            quiet.stop()
        self._patch.stop()
        self.tmp.cleanup()

    def test_auto_unlock_stores_the_passphrase_for_systemd(self):
        with patch.object(wiz, "choose", return_value=1):
            self.assertTrue(wiz.step_unlock_mode("my-passphrase"))
        self.assertIn("SLCW_VAULT_PASSPHRASE=my-passphrase", self.key_path.read_text())
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)

    def test_manual_unlock_stores_nothing(self):
        with patch.object(wiz, "choose", return_value=2):
            self.assertFalse(wiz.step_unlock_mode("my-passphrase"))
        self.assertFalse(self.key_path.exists())

    def test_switching_to_manual_removes_a_previously_stored_key(self):
        with patch.object(wiz, "choose", return_value=1):
            wiz.step_unlock_mode("my-passphrase")
        with patch.object(wiz, "choose", return_value=2):
            wiz.step_unlock_mode("my-passphrase")
        self.assertFalse(self.key_path.exists(),
                         "an abandoned passphrase must not stay on disk")


class ServiceUnitTests(unittest.TestCase):
    def setUp(self):
        unit = (Path(__file__).resolve().parent.parent / "slcw-fleet.service").read_text()
        self.directives = [line.strip() for line in unit.splitlines()
                           if line.strip() and not line.strip().startswith("#")]

    def test_unit_file_points_at_the_project_venv(self):
        exec_lines = [d for d in self.directives if d.startswith("ExecStart=")]
        self.assertEqual(len(exec_lines), 1)
        self.assertIn("/root/slcw-bot/.venv/bin/python", exec_lines[0])
        self.assertNotIn("/tmp/", exec_lines[0],
                         "a venv under /tmp would not survive a reboot")

    def test_unit_restarts_on_failure(self):
        self.assertIn("Restart=always", self.directives)
        self.assertIn("WantedBy=multi-user.target", self.directives)


class RequirementsTests(unittest.TestCase):
    def test_every_third_party_import_is_pinned(self):
        root = Path(__file__).resolve().parent.parent
        pinned = {line.split("==")[0].lower().replace("_", "-")
                  for line in (root / "requirements.txt").read_text().splitlines()
                  if line and not line.startswith("#")}
        # Import names differ from distribution names for these two.
        aliases = {"curl-cffi": "curl-cffi", "solders": "solders",
                   "base58": "base58", "cryptography": "cryptography"}
        for distribution in aliases.values():
            self.assertIn(distribution, pinned, f"{distribution} is not pinned")

    def test_installer_installs_from_requirements(self):
        installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
        self.assertIn("-r requirements.txt", installer)
        self.assertIn("python3 -m venv .venv", installer)


if __name__ == "__main__":
    unittest.main()
