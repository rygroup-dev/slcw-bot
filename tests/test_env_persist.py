import os
import tempfile
import unittest
from pathlib import Path

from slcw.config import parse_env_text, persist_overrides


class PersistOverridesTests(unittest.TestCase):
    """Telegram toggles have to outlive a restart.

    Before this, every switch in the economy menu was a dataclasses.replace on
    the in-memory Config: it changed behaviour immediately and then silently
    reverted to .env the next time the service started.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / ".env"

    def test_an_existing_key_is_updated_in_place(self):
        self.path.write_text("SLCW_ENABLED=true\nSLCW_FARMING_GOLD=false\n")
        persist_overrides({"SLCW_FARMING_GOLD": "true"}, path=self.path)
        self.assertEqual(parse_env_text(self.path.read_text())["SLCW_FARMING_GOLD"],
                         "true")

    def test_a_missing_key_is_appended(self):
        self.path.write_text("SLCW_ENABLED=true\n")
        persist_overrides({"SLCW_FARMING_GOLD_HOURS": "4"}, path=self.path)
        self.assertEqual(parse_env_text(self.path.read_text())["SLCW_FARMING_GOLD_HOURS"],
                         "4")

    def test_unrelated_keys_survive(self):
        self.path.write_text(
            "TELEGRAM_BOT_TOKEN=secret-token\nSLCW_FIREBASE_API_KEY=abc123\n"
            "SLCW_FARMING_GOLD=false\n")
        persist_overrides({"SLCW_FARMING_GOLD": "true"}, path=self.path)
        values = parse_env_text(self.path.read_text())
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "secret-token")
        self.assertEqual(values["SLCW_FIREBASE_API_KEY"], "abc123")

    def test_comments_and_blank_lines_survive(self):
        self.path.write_text("# master switches\n\nSLCW_FARMING_GOLD=false\n")
        persist_overrides({"SLCW_FARMING_GOLD": "true"}, path=self.path)
        text = self.path.read_text()
        self.assertIn("# master switches", text)
        self.assertIn("\n\n", text)

    def test_a_key_is_not_duplicated(self):
        self.path.write_text("SLCW_FARMING_GOLD=false\n")
        persist_overrides({"SLCW_FARMING_GOLD": "true"}, path=self.path)
        persist_overrides({"SLCW_FARMING_GOLD": "false"}, path=self.path)
        body = self.path.read_text()
        self.assertEqual(body.count("SLCW_FARMING_GOLD="), 1)

    def test_the_file_stays_private(self):
        self.path.write_text("SLCW_FARMING_GOLD=false\n")
        persist_overrides({"SLCW_FARMING_GOLD": "true"}, path=self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_writing_to_a_missing_file_creates_it(self):
        persist_overrides({"SLCW_FARMING_GOLD": "true"}, path=self.path)
        self.assertEqual(parse_env_text(self.path.read_text())["SLCW_FARMING_GOLD"],
                         "true")

    def test_the_live_process_environment_is_updated_too(self):
        """A restart re-reads .env, but the running process must not go stale."""
        self.addCleanup(os.environ.pop, "SLCW_TEST_ONLY_KEY", None)
        persist_overrides({"SLCW_TEST_ONLY_KEY": "9"}, path=self.path)
        self.assertEqual(os.environ["SLCW_TEST_ONLY_KEY"], "9")


class QuotedValueTests(unittest.TestCase):
    """A clan name has a space in it, and systemd would split on that."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / ".env"

    def test_a_value_with_a_space_is_written_quoted(self):
        persist_overrides({"SLCW_CLAN_NAME": "RY Group"}, path=self.path)
        self.assertIn('SLCW_CLAN_NAME="RY Group"', self.path.read_text())

    def test_and_reads_back_without_the_quotes(self):
        persist_overrides({"SLCW_CLAN_NAME": "RY Group"}, path=self.path)
        self.assertEqual(parse_env_text(self.path.read_text())["SLCW_CLAN_NAME"],
                         "RY Group")

    def test_a_plain_value_is_left_unquoted(self):
        persist_overrides({"SLCW_CLAN_TAG": "RYG"}, path=self.path)
        self.assertIn("SLCW_CLAN_TAG=RYG", self.path.read_text())
        self.assertNotIn('"RYG"', self.path.read_text())

    def test_an_existing_quoted_value_is_read_unquoted(self):
        self.path.write_text("SLCW_CLAN_NAME='RY Group'\n")
        self.assertEqual(parse_env_text(self.path.read_text())["SLCW_CLAN_NAME"],
                         "RY Group")


if __name__ == "__main__":
    unittest.main()
