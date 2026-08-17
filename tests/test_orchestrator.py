import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slcw import economy as econ
from slcw.combat import CombatMemory
from slcw.config import Config
from slcw.market import build_snapshot
from slcw.model import parse_player
from slcw.orchestrator import Orchestrator
from slcw.transport import ApiError


class FakeApi:
    """Records calls instead of touching the network."""

    def __init__(self, turn_script=None):
        self.calls = []
        self.turn_script = turn_script or []
        self.turn_index = 0

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return {"success": True}

    def finish_activity(self, session):
        self.calls.append(("finishActivity", {}))
        return {"success": True, "rewardSummary": {"type": "farming", "gold": 1000}}

    def claim_initial_reward(self, session, level):
        return self._record("claimInitialReward", level=level)

    def start_relax(self, session):
        return self._record("startRelax")

    def start_production(self, session, cycles=1):
        return self._record("startProduction", cycles=cycles)

    def spend_attribute_points(self, session, target_type, target_id, amount=1):
        return self._record("spendAttributePoints", target_id=target_id, amount=amount)

    def start_travel(self, session, destination_id):
        return self._record("startTravel", destinationId=destination_id)

    def start_battle(self, session, monster_id):
        self.calls.append(("startBattle", {"monsterId": monster_id}))
        return {"battleId": "battle-1"}

    def start_task_battle(self, session):
        self.calls.append(("startTaskBattle", {}))
        return {"battleId": "task-battle-1"}

    def complete_newbie_quest(self, session):
        return self._record("completeNewbieQuest")

    def process_turn(self, session, battle_id, attack, defense):
        self.calls.append(("processTurn", {"attack": attack, "defense": defense}))
        if self.turn_index < len(self.turn_script):
            outcome = self.turn_script[self.turn_index]
            self.turn_index += 1
            return outcome
        return {"turnResult": {"player": {"zone": attack, "type": "hit"},
                               "monster": {"zone": "head", "type": "hit"}},
                "isOver": False}


def state_of(**overrides):
    doc = {
        # Level 15 at grade 1 sits on the grade cap, so no free level-up is
        # available and each test exercises the behaviour it names.
        "level": 15, "xp": 1398, "balance": 0, "energy": 85, "maxEnergy": 100,
        "currentHealth": 130, "currentMana": 130, "attributePoints": 0,
        "currentLocationId": "city_2",
        "attributes": {"wisdom": 3, "vitality": 3},
        "claimedInitialRewardsV2": list(range(1, 16)),
        "newbieQuest": 999, "activity": None,
    }
    doc.update(overrides)
    return parse_player(doc)


def make(config=None, api=None):
    tmp = tempfile.mkdtemp()
    return Orchestrator(
        config=config or Config(enabled=True, dry_run=False),
        api=api or FakeApi(),
        combat=CombatMemory(path=Path(tmp) / "combat.json"),
    )


class PriorityTests(unittest.TestCase):
    def test_expired_activity_is_claimed_before_anything_else(self):
        api = FakeApi()
        orchestrator = make(api=api)
        state = state_of(activity={"type": "production", "endTime": {"seconds": 1}})
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "finishActivity")
        self.assertEqual(api.calls[0][0], "finishActivity")

    def test_running_activity_yields_no_action(self):
        orchestrator = make()
        state = state_of(activity={"type": "production", "endTime": {"seconds": 9999999999}})
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "idle")
        self.assertIn("running", decision.reason)

    def test_unclaimed_level_reward_taken_before_gameplay(self):
        api = FakeApi()
        orchestrator = make(api=api)
        state = state_of(claimedInitialRewardsV2=[1, 2])
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "claimInitialReward")
        self.assertEqual(decision.params["level"], 3)

    def test_pending_newbie_quest_is_completed_before_gameplay(self):
        api = FakeApi()
        orchestrator = make(api=api)
        state = state_of(newbieQuest=6)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "completeNewbieQuest")
        self.assertEqual(api.calls[0], ("completeNewbieQuest", {}))

    def test_newbie_quest_is_capped_so_it_cannot_stall_the_fleet_forever(self):
        from slcw.orchestrator import NEWBIE_QUEST_MAX_ATTEMPTS
        orchestrator = make(api=FakeApi())
        state = state_of(newbieQuest=NEWBIE_QUEST_MAX_ATTEMPTS)
        candidates = orchestrator.build_candidates(state)
        self.assertNotIn("completeNewbieQuest", [c.action for c in candidates])

    def test_unspent_attribute_points_are_used(self):
        api = FakeApi()
        orchestrator = make(api=api)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state_of(attributePoints=2))
        self.assertEqual(decision.action, "spendAttributePoints")


class SelectionTests(unittest.TestCase):
    def test_city_runs_production(self):
        api = FakeApi()
        orchestrator = make(config=Config(enabled=True, dry_run=False,
                                          auto_travel=False), api=api)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state_of())
        self.assertEqual(decision.action, "startProduction")
        self.assertEqual(api.calls[0][1]["cycles"], 1)

    def test_travel_beats_production_once_repeats_are_counted(self):
        """Arriving somewhere to fight means fighting until energy runs out.

        Charging the whole walk against a single battle made travel look
        worthless and pinned every wallet to whatever it started doing.
        """
        orchestrator = make()
        candidates = orchestrator.build_candidates(state_of())
        top = candidates[0]
        self.assertEqual(top.action, "startTravel")
        self.assertIn("battle", top.reason)
        self.assertIn("×", top.reason, "the projection must show the repeat count")

    def test_low_health_prefers_rest_over_production(self):
        orchestrator = make()
        state = state_of(currentHealth=40)  # 40/130 is below the 0.55 rest ratio
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "startRelax")

    def test_never_battles_below_the_health_floor(self):
        orchestrator = make()
        state = state_of(currentLocationId="farm_3", currentHealth=50)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertNotEqual(decision.action, "battle")

    def test_disabled_engine_still_claims_but_never_plays(self):
        api = FakeApi()
        orchestrator = make(config=Config(enabled=False, dry_run=False), api=api)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state_of())
        self.assertNotEqual(decision.action, "startProduction")

    def test_dry_run_never_calls_the_api(self):
        api = FakeApi()
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=api)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state_of())
        self.assertEqual(decision.result, {"dry_run": True})
        self.assertEqual(api.calls, [])

    def test_market_prices_lift_battle_above_production(self):
        orders = [
            {"status": "open", "type": "buy", "templateId": "spiderfang",
             "price": 5000, "quantity": 50, "filled": 0},
        ]
        orchestrator = make()
        state = state_of(currentLocationId="farm_3")
        candidates = orchestrator.build_candidates(state, build_snapshot(orders))
        self.assertEqual(candidates[0].action, "battle")
        self.assertFalse(candidates[0].degraded)


class BattleTests(unittest.TestCase):
    def test_battle_settles_activity_on_normal_win(self):
        api = FakeApi(turn_script=[
            {"turnResult": {"player": {"zone": "head", "type": "hit"},
                            "monster": {"zone": "legs", "type": "hit"}}, "isOver": False},
            {"turnResult": {"player": {"zone": "head", "type": "hit"},
                            "monster": {"zone": "legs", "type": "hit"}}, "isOver": True},
        ])
        orchestrator = make(api=api)
        with patch("slcw.orchestrator.time.sleep"):
            result = orchestrator.run_battle(None, "forestspider_lvl1_2")
        self.assertEqual(result["turns"], 2)
        self.assertFalse(result["reached_turn_cap"])
        self.assertIn("finishActivity", [c[0] for c in api.calls])

    def test_turn_cap_still_settles_the_activity(self):
        # The old engine raised here without calling finishActivity, leaving the
        # battle open server-side and wasting the energy.
        api = FakeApi()
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, battle_max_turns=3), api=api)
        with patch("slcw.orchestrator.time.sleep"):
            result = orchestrator.run_battle(None, "forestspider_lvl1_2")
        self.assertTrue(result["reached_turn_cap"])
        self.assertEqual(result["turns"], 3)
        self.assertIn("finishActivity", [c[0] for c in api.calls])

    def test_battle_learns_from_observed_turns(self):
        api = FakeApi(turn_script=[
            {"turnResult": {"player": {"zone": "head", "type": "blocked"},
                            "monster": {"zone": "torso", "type": "hit"}}, "isOver": True},
        ])
        orchestrator = make(api=api)
        with patch("slcw.orchestrator.time.sleep"):
            orchestrator.run_battle(None, "spider")
        model = orchestrator.combat.model_for("spider")
        self.assertEqual(model.attacks_blocked["head"], 1)
        self.assertEqual(model.monster_attacks["torso"], 1)


class ErrorHandlingTests(unittest.TestCase):
    def test_already_claimed_is_not_an_error(self):
        class Rejecting(FakeApi):
            def claim_initial_reward(self, session, level):
                raise ApiError("Reward already claimed",
                               status_code="ALREADY_EXISTS", http_status=409)

        orchestrator = make(api=Rejecting())
        state = state_of(claimedInitialRewardsV2=[1])
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        # This is the audited bug: the old code matched the substring "already
        # claimed", never saw ALREADY_EXISTS, and escalated into a breaker trip.
        self.assertEqual(decision.error, "")
        self.assertIn("already_done", decision.result)

    def test_real_server_failure_is_reported(self):
        class Failing(FakeApi):
            def start_production(self, session, cycles=1):
                raise ApiError("Internal error", status_code="INTERNAL", http_status=500)

        orchestrator = make(config=Config(enabled=True, dry_run=False,
                                          auto_travel=False), api=Failing())
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state_of())
        self.assertIn("INTERNAL", decision.error)


class RationaleTests(unittest.TestCase):
    def test_decision_explains_every_candidate_considered(self):
        orchestrator = make()
        state = state_of(currentLocationId="farm_3", currentHealth=60)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        lines = decision.rationale_lines()
        self.assertTrue(lines[0].startswith("Chose:"))
        self.assertGreaterEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
