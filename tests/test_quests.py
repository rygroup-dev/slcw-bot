import json
import tempfile
import unittest
from pathlib import Path

from slcw.quests import MAX_STEPS, RETRY_AFTER_S, NewbieQuestMemory


class NewbieQuestMemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "newbie_quests.json"

    def memory(self):
        return NewbieQuestMemory(path=self.path)

    def test_an_untried_wallet_may_attempt_the_chain(self):
        self.assertTrue(self.memory().is_available("w1"))

    def test_a_rejected_attempt_parks_the_chain(self):
        mem = self.memory()
        mem.record_failure("w1", "Insufficient items: 0/1", now=1000)
        self.assertFalse(mem.is_available("w1", now=1000))

    def test_the_chain_reopens_once_the_cooldown_passes(self):
        mem = self.memory()
        mem.record_failure("w1", "Insufficient items: 0/1", now=1000)
        self.assertTrue(mem.is_available("w1", now=1000 + RETRY_AFTER_S + 1))

    def test_one_wallets_failure_does_not_park_another(self):
        mem = self.memory()
        mem.record_failure("w1", "Insufficient items: 0/1", now=1000)
        self.assertTrue(mem.is_available("w2", now=1000))

    def test_a_success_clears_the_cooldown_and_advances_the_step(self):
        mem = self.memory()
        mem.record_failure("w1", "Insufficient items: 0/1", now=1000)
        mem.record_success("w1")
        self.assertTrue(mem.is_available("w1", now=1000))
        self.assertEqual(mem.wallets["w1"]["steps"], 1)

    def test_the_chain_closes_after_the_maximum_number_of_steps(self):
        mem = self.memory()
        for _ in range(MAX_STEPS):
            mem.record_success("w1")
        self.assertFalse(mem.is_available("w1"))

    def test_state_survives_a_restart(self):
        self.memory().record_failure("w1", "Insufficient items: 0/1", now=1000)
        self.assertFalse(self.memory().is_available("w1", now=1000))

    def test_a_corrupt_memory_file_is_ignored_rather_than_fatal(self):
        self.path.write_text("{not json")
        self.assertTrue(self.memory().is_available("w1"))

    def test_the_file_is_written_private(self):
        mem = self.memory()
        mem.record_failure("w1", "nope", now=1000)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("w1", json.loads(self.path.read_text()))


if __name__ == "__main__":
    unittest.main()
