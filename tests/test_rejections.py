import tempfile
import unittest
from pathlib import Path

from slcw import rejections


def memory():
    return rejections.RejectionMemory(
        path=Path(tempfile.mkdtemp()) / "rejections.json")


class FingerprintTests(unittest.TestCase):
    def test_the_same_call_hashes_the_same_way(self):
        self.assertEqual(
            rejections.fingerprint("equipItem", {"instanceId": "a"}),
            rejections.fingerprint("equipItem", {"instanceId": "a"}))

    def test_different_arguments_are_different_calls(self):
        self.assertNotEqual(
            rejections.fingerprint("equipItem", {"instanceId": "a"}),
            rejections.fingerprint("equipItem", {"instanceId": "b"}))

    def test_key_order_does_not_matter(self):
        self.assertEqual(
            rejections.fingerprint("openChests", {"a": 1, "b": 2}),
            rejections.fingerprint("openChests", {"b": 2, "a": 1}))


class ParkingTests(unittest.TestCase):
    def test_a_fresh_call_is_not_parked(self):
        self.assertFalse(memory().is_parked("w1", "equipItem", {"instanceId": "a"}))

    def test_a_refused_call_is_parked(self):
        mem = memory()
        mem.park("w1", "equipItem", {"instanceId": "a"}, "grade too low")
        self.assertTrue(mem.is_parked("w1", "equipItem", {"instanceId": "a"}))

    def test_parking_is_per_wallet(self):
        mem = memory()
        mem.park("w1", "equipItem", {"instanceId": "a"}, "grade too low")
        self.assertFalse(mem.is_parked("w2", "equipItem", {"instanceId": "a"}))

    def test_parking_is_per_argument(self):
        mem = memory()
        mem.park("w1", "equipItem", {"instanceId": "a"}, "grade too low")
        self.assertFalse(mem.is_parked("w1", "equipItem", {"instanceId": "b"}))

    def test_the_park_expires(self):
        """A precondition can clear later — inventory frees up, grade rises."""
        mem = memory()
        mem.park("w1", "openChests", {}, "no space", now=0)
        self.assertTrue(mem.is_parked("w1", "openChests", {}, now=1))
        self.assertFalse(
            mem.is_parked("w1", "openChests", {}, now=rejections.RETRY_AFTER_S + 1))

    def test_a_success_clears_the_park(self):
        mem = memory()
        mem.park("w1", "openChests", {}, "no space")
        mem.clear("w1", "openChests", {})
        self.assertFalse(mem.is_parked("w1", "openChests", {}))

    def test_it_survives_a_restart(self):
        mem = memory()
        mem.park("w1", "equipItem", {"instanceId": "a"}, "grade too low")
        mem.save()
        reloaded = rejections.RejectionMemory(path=mem.path)
        self.assertTrue(reloaded.is_parked("w1", "equipItem", {"instanceId": "a"}))

    def test_a_corrupt_file_is_ignored_rather_than_fatal(self):
        mem = memory()
        mem.path.parent.mkdir(parents=True, exist_ok=True)
        mem.path.write_text("{not json")
        rejections.RejectionMemory(path=mem.path)


class ParkableTests(unittest.TestCase):
    def test_free_value_actions_can_be_parked(self):
        for action in ("openChests", "equipItem", "upgradeEquip",
                       "claimInitialReward", "applyClan", "generateClanQuest"):
            self.assertIn(action, rejections.PARKABLE, action)

    def test_battles_are_never_parked(self):
        """Parking a battle would leave the wallet busy forever: an open fight
        has to be resolved before anything else can be chosen at all."""
        for action in ("resumeBattle", "finishActivity", "battle",
                       "startTaskBattle"):
            self.assertNotIn(action, rejections.PARKABLE, action)
