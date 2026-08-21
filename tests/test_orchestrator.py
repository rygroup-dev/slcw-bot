import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slcw import economy as econ
from slcw.combat import CombatMemory
from slcw.quests import NewbieQuestMemory
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
        # (action, status_code, message) to reject, so a test can reproduce a
        # server refusal such as completeNewbieQuest's "Insufficient items".
        self.fail_with = None

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail_with and self.fail_with[0] == name:
            _, status_code, message = self.fail_with
            raise ApiError(message, status_code=status_code)
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
        quests=NewbieQuestMemory(path=Path(tmp) / "newbie_quests.json"),
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

    def test_hunting_is_offered_outside_battle_locations(self):
        """Unlike battle, hunting is not gated to farm_3/wildland_1."""
        orchestrator = make()
        state = state_of(currentLocationId="farm_1", energy=50, balance=1000)
        candidates = orchestrator.build_candidates(state)
        self.assertIn("startHunting", [c.action for c in candidates])

    def test_hunting_and_battle_target_the_same_monster(self):
        """Hunting is not hardcoded — it tracks whatever select_monster picks,
        which is why it scales with level, stats and equipment over time."""
        orchestrator = make()
        state = state_of(currentLocationId="farm_3", energy=50, balance=1000)
        candidates = orchestrator.build_candidates(state)
        by_action = {c.action: c for c in candidates}
        self.assertIn("battle", by_action)
        self.assertIn("startHunting", by_action)
        self.assertEqual(by_action["battle"].params["monsterId"],
                         by_action["startHunting"].params["monsterId"])

    def test_hunting_uses_learned_xp_once_the_monster_has_been_fought(self):
        api = FakeApi()
        orchestrator = make(api=api)
        orchestrator.combat.record_battle(
            "forestspider_lvl1_2", {"winner": "player", "xp": 40}, turns=3, damage_taken=5)
        # Deterministic: never explore an untried monster, so the only
        # measured one (forestspider, just recorded above) is always picked.
        orchestrator.rng.random = lambda: 1.0
        state = state_of(currentLocationId="farm_1", energy=50, balance=1000)
        candidates = orchestrator.build_candidates(state)
        hunt = next(c for c in candidates if c.action == "startHunting")
        self.assertEqual(hunt.params["monsterId"], "forestspider_lvl1_2")
        self.assertAlmostEqual(hunt.gold_equivalent,
                               40 * econ.HUNTING_YIELD_RATIO * econ.DEFAULT_XP_GOLD)


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


class StuckBattleRecoveryTests(unittest.TestCase):
    """A battle left open server-side must be settled, not waited on."""

    def _battle_state(self):
        return parse_player({
            "level": 11, "energy": 80, "maxEnergy": 100,
            "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 16)),
            "activity": {
                "type": "battle", "activityId": "abc",
                "startTime": "2026-08-16T11:20:50.319Z",
                "data": {"battleId": "abc", "monsterId": "bigfrog_lvl1_1"},
            },
        })

    def test_open_battle_is_resumed_instead_of_treated_as_in_progress(self):
        api = FakeApi(turn_script=[{"turnResult": {}, "isOver": True}])
        decision = make(api=api).decide_and_act({"id": "w1"}, None, self._battle_state())
        self.assertEqual(decision.action, "resumeBattle")

    def test_the_wallet_is_not_reported_idle_while_a_battle_sits_unclaimed(self):
        api = FakeApi(turn_script=[{"turnResult": {}, "isOver": True}])
        decision = make(api=api).decide_and_act({"id": "w1"}, None, self._battle_state())
        self.assertNotEqual(decision.action, "idle")

    def test_resuming_fights_the_open_battle_out_before_settling_it(self):
        """finishActivity alone returns HTTP 500 while the fight is unresolved.

        Measured live on wallet-13: its battle was open at round 12, blind
        settling was refused, and one processTurn ended it — after which the
        same finishActivity paid out 42 xp and 2 livingwood.
        """
        api = FakeApi(turn_script=[{"turnResult": {}, "isOver": True}])
        make(api=api).decide_and_act({"id": "w1"}, None, self._battle_state())
        names = [c[0] for c in api.calls]
        self.assertIn("processTurn", names)
        self.assertIn("finishActivity", names)
        self.assertLess(names.index("processTurn"), names.index("finishActivity"))

    def test_resuming_does_not_start_a_second_battle(self):
        api = FakeApi(turn_script=[{"turnResult": {}, "isOver": True}])
        make(api=api).decide_and_act({"id": "w1"}, None, self._battle_state())
        self.assertNotIn("startBattle", [c[0] for c in api.calls])

    def test_an_expired_timed_activity_is_still_settled_directly(self):
        api = FakeApi()
        state = parse_player({
            "level": 11, "energy": 80, "maxEnergy": 100,
            "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 16)),
            "activity": {"type": "farming", "activityId": "f1",
                         "endTime": "2020-01-01T00:00:00Z"},
        })
        decision = make(api=api).decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "finishActivity")
        self.assertNotIn("processTurn", [c[0] for c in api.calls])


class NewbieQuestRetryTests(unittest.TestCase):
    """The chain must not be re-picked after the server has refused it."""

    def _fresh_state(self):
        return state_of(newbieQuest=0)

    def test_a_refused_chain_is_not_retried_on_the_next_cycle(self):
        api = FakeApi()
        api.fail_with = ("completeNewbieQuest", "FAILED_PRECONDITION",
                         "Insufficient items: 0/1")
        orchestrator = make(api=api)
        first = orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        self.assertEqual(first.action, "completeNewbieQuest")

        second = orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        self.assertNotEqual(second.action, "completeNewbieQuest")

    def test_a_benign_refusal_still_parks_the_chain(self):
        api = FakeApi()
        api.fail_with = ("completeNewbieQuest", "FAILED_PRECONDITION",
                         "Insufficient items: 0/1")
        orchestrator = make(api=api)
        orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        self.assertFalse(orchestrator.quests.is_available("w1"))

    def test_a_refusal_for_one_wallet_does_not_park_another(self):
        api = FakeApi()
        api.fail_with = ("completeNewbieQuest", "FAILED_PRECONDITION",
                         "Insufficient items: 0/1")
        orchestrator = make(api=api)
        orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        self.assertTrue(orchestrator.quests.is_available("w2"))

    def test_a_parked_chain_frees_the_wallet_for_real_work(self):
        api = FakeApi()
        api.fail_with = ("completeNewbieQuest", "FAILED_PRECONDITION",
                         "Insufficient items: 0/1")
        orchestrator = make(api=api)
        orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        second = orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        self.assertNotIn(second.action, ("idle", "completeNewbieQuest"))

    def test_a_successful_step_leaves_the_chain_open(self):
        orchestrator = make(api=FakeApi())
        orchestrator.decide_and_act({"id": "w1"}, None, self._fresh_state())
        self.assertTrue(orchestrator.quests.is_available("w1"))
