import dataclasses
import unittest

from slcw import tasks
from slcw.config import Config
from slcw.model import parse_player
from tests.test_orchestrator import FakeApi, make

RAW_TASK = {"taskIndex": 3, "monsterId": "forestspider_lvl1_2", "monsterLevel": 1,
            "killsRequired": 10, "killsProgress": 4, "status": "active",
            "goldReward": 5000}


class ParsingTests(unittest.TestCase):
    def test_parses_a_task(self):
        task = tasks.parse_task(RAW_TASK)
        self.assertEqual(task.index, 3)
        self.assertEqual(task.kills_remaining, 6)
        self.assertEqual(task.gold_reward, 5000)
        self.assertAlmostEqual(task.gold_per_kill, 500)

    def test_empty_task_is_none(self):
        self.assertIsNone(tasks.parse_task({}))
        self.assertIsNone(tasks.parse_task(None))

    def test_zero_kills_required_does_not_divide_by_zero(self):
        task = tasks.parse_task(dict(RAW_TASK, killsRequired=0))
        self.assertEqual(task.gold_per_kill, 0.0)

    def test_parses_status_payload(self):
        status = tasks.parse_status({
            "playerLevel": 12, "completedCount": 3, "allDone": False,
            "hasActiveTask": True, "task": RAW_TASK})
        self.assertEqual(status.player_level, 12)
        self.assertTrue(status.has_active_task)
        self.assertEqual(status.task.index, 3)

    def test_missing_payload_yields_a_safe_default(self):
        status = tasks.parse_status(None)
        self.assertEqual(status.player_level, 1)
        self.assertIsNone(status.task)
        self.assertFalse(status.eligible)


class EligibilityTests(unittest.TestCase):
    def _status(self, **overrides):
        payload = {"playerLevel": 12, "completedCount": 1, "allDone": False,
                   "hasActiveTask": False, "task": None}
        payload.update(overrides)
        return tasks.parse_status(payload)

    def test_level_ten_is_the_gate(self):
        self.assertFalse(self._status(playerLevel=9).eligible)
        self.assertTrue(self._status(playerLevel=10).eligible)

    def test_all_done_ends_eligibility(self):
        self.assertFalse(self._status(allDone=True).eligible)

    def test_can_accept_only_without_an_active_task(self):
        self.assertTrue(self._status().can_accept)
        self.assertFalse(self._status(hasActiveTask=True).can_accept)

    def test_can_claim_only_when_completed(self):
        completed = self._status(hasActiveTask=True,
                                 task=dict(RAW_TASK, status="completed"))
        active = self._status(hasActiveTask=True, task=RAW_TASK)
        self.assertTrue(completed.can_claim)
        self.assertFalse(active.can_claim)
        self.assertTrue(active.can_fight)


class OrchestratorTests(unittest.TestCase):
    def _state(self, level=12):
        return parse_player({
            "level": level, "grade": 1, "energy": 80, "maxEnergy": 100,
            "balance": 5000, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_2", "attributePoints": 0,
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 13)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})

    def _status(self, **overrides):
        payload = {"playerLevel": 12, "completedCount": 2, "allDone": False,
                   "hasActiveTask": False, "task": None}
        payload.update(overrides)
        return tasks.parse_status(payload)

    def test_completed_task_reward_is_claimed_first(self):
        api = FakeApi()
        api.claim_task_reward = lambda s: api._record("claimTaskReward")
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        status = self._status(hasActiveTask=True,
                              task=dict(RAW_TASK, status="completed"))
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, status)
        self.assertEqual(decision.action, "claimTaskReward")
        self.assertIn("5,000", decision.reason)

    def test_next_task_is_accepted_when_none_is_active(self):
        api = FakeApi()
        api.accept_task = lambda s: api._record("acceptTask")
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, self._status())
        self.assertEqual(decision.action, "acceptTask")

    def test_below_level_ten_tasks_are_ignored(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        status = self._status(playerLevel=5)
        candidates = orchestrator.build_candidates(
            self._state(level=5), task_status=status)
        self.assertNotIn("acceptTask", [c.action for c in candidates])

    def test_no_task_status_does_not_break_the_cycle(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(self._state(), task_status=None)
        self.assertTrue(candidates)

    def test_exhausted_ladder_is_left_alone(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state(), task_status=self._status(allDone=True))
        self.assertNotIn("acceptTask", [c.action for c in candidates])

    def test_active_task_is_fought_not_left_to_the_normal_battle_picker(self):
        api = FakeApi()
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        status = self._status(hasActiveTask=True, task=RAW_TASK)
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, status)
        self.assertEqual(decision.action, "startTaskBattle")
        self.assertEqual(decision.params["monsterId"], "forestspider_lvl1_2")
        self.assertEqual(api.calls[0], ("startTaskBattle", {}))
        self.assertIn("4/10", decision.reason)

    def test_active_task_battle_defers_to_rest_at_low_health(self):
        api = FakeApi()
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        status = self._status(hasActiveTask=True, task=RAW_TASK)
        low_health = dataclasses.replace(self._state(), health=1)
        candidates = orchestrator.build_candidates(low_health, task_status=status)
        self.assertNotIn("startTaskBattle", [c.action for c in candidates])


if __name__ == "__main__":
    unittest.main()
