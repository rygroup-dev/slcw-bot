import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slcw import economy as econ
from slcw.combat import CombatMemory
from slcw.quests import NewbieQuestMemory
from slcw.rejections import RejectionMemory
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

    def open_chests(self, session, chest_template_id, quantity=1):
        return self._record("openChests", chestTemplateId=chest_template_id,
                            quantity=quantity)

    def execute_black_market_order(self, session, resource_id, action, quantity):
        return self._record("executeBlackMarketOrder", resourceId=resource_id,
                            action=action, quantity=quantity)

    def pay_city_entry_fee(self, session, city_id):
        return self._record("payCityEntryFee", cityId=city_id)

    def purchase_imperial_seal(self, session, quantity=1):
        return self._record("purchaseImperialSeal", quantity=quantity)

    def evolve_grade(self, session):
        return self._record("evolveGrade")

    def sell_equipment_item(self, session, instance_id):
        return self._record("sellEquipmentItem", instanceId=instance_id)

    def unequip_item(self, session, slot_name):
        return self._record("unequipItem", slotName=slot_name)

    def equip_item(self, session, instance_id):
        return self._record("equipItem", instanceId=instance_id)

    def start_relax(self, session):
        return self._record("startRelax")

    def start_production(self, session, cycles=1):
        return self._record("startProduction", cycles=cycles)

    def spend_attribute_points(self, session, target_type, target_id, amount=1):
        return self._record("spendAttributePoints", target_id=target_id, amount=amount)

    def start_travel(self, session, destination_id):
        return self._record("startTravel", destinationId=destination_id)

    def dispatch_caravan(self, session, template_id, quantity, destination_id):
        return self._record("dispatchCaravan", templateId=template_id,
                            quantity=quantity, destinationId=destination_id)

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
        if self.fail_with and self.fail_with[0] == "processTurn":
            _, status_code, message = self.fail_with
            raise ApiError(message, status_code=status_code)
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
        rejections=RejectionMemory(path=Path(tmp) / "rejections.json"),
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
        """Unlike battle, hunting is not gated to farm_3/wildland_1.

        On a full bar: hunting buys 11 xp with three energy where a battle buys
        22 with one, so once energy is scarce enough to carry a price it is
        correctly outbid. Half a bar used to be enough at an xp worth 8 gold
        and is not at 5.
        """
        orchestrator = make()
        state = state_of(currentLocationId="farm_1", energy=100, balance=1000)
        candidates = orchestrator.build_candidates(state)
        self.assertIn("startHunting", [c.action for c in candidates])

    def test_hunting_and_battle_target_the_same_monster(self):
        """Hunting is not hardcoded — it tracks whatever select_monster picks,
        which is why it scales with level, stats and equipment over time."""
        orchestrator = make()
        state = state_of(currentLocationId="farm_3", energy=100, balance=1000)
        candidates = orchestrator.build_candidates(state)
        by_action = {c.action: c for c in candidates}
        self.assertIn("battle", by_action)
        self.assertIn("startHunting", by_action)
        self.assertEqual(by_action["battle"].params["monsterId"],
                         by_action["startHunting"].params["monsterId"])

    def test_hunting_uses_learned_xp_once_the_monster_has_been_fought(self):
        api = FakeApi()
        orchestrator = make(api=api)
        # Within reach of the level-15 fixture: the server refuses anything
        # more than five levels down, so a level-1 monster could never be the
        # answer here whatever the memory says about it.
        orchestrator.combat.record_battle(
            "bigfrog_lvl13_2", {"winner": "player", "xp": 40}, turns=3, damage_taken=5)
        # Deterministic: never explore an untried monster, so the only
        # measured one (the frog, just recorded above) is always picked.
        orchestrator.rng.random = lambda: 1.0
        state = state_of(currentLocationId="farm_1", energy=50, balance=1000)
        candidates = orchestrator.build_candidates(state)
        hunt = next(c for c in candidates if c.action == "startHunting")
        self.assertEqual(hunt.params["monsterId"], "bigfrog_lvl13_2")
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


class RejectionParkingTests(unittest.TestCase):
    """A free action the server keeps refusing must stop being chosen.

    FAILED_PRECONDITION is benign, and the free-value branches return early, so
    without this a refused action is re-picked every cycle forever — the wallet
    goes silent while the fleet view still shows zero errors.
    """

    def _state(self):
        return parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100,
            "balance": 5000, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999,
            "activity": None, "freeEnergyRefillsToday": 3,
            "lastFreeEnergyRefillDate": "2099-01-01"})

    def _chest_inventory(self):
        from slcw import inventory as inv
        return inv.parse_inventory({"maxSlots": 20, "slots": [
            {"slotIndex": 0, "templateId": "small_equip_chest",
             "quantity": 3, "instanceId": None}]})

    def test_a_refused_free_action_is_not_chosen_again(self):
        api = FakeApi()
        api.fail_with = ("openChests", "FAILED_PRECONDITION",
                         "Not enough space in inventory")
        orchestrator = make(api=api)
        inventory = self._chest_inventory()

        first = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, None, inventory)
        self.assertEqual(first.action, "openChests")
        self.assertIn("already_done", first.result)

        second = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, None, inventory)
        self.assertNotEqual(second.action, "openChests")

    def test_the_park_is_per_wallet(self):
        api = FakeApi()
        api.fail_with = ("openChests", "FAILED_PRECONDITION", "Not enough space")
        orchestrator = make(api=api)
        inventory = self._chest_inventory()
        orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, None, inventory)
        second = orchestrator.decide_and_act(
            {"id": "w2"}, None, self._state(), None, None, None, inventory)
        self.assertEqual(second.action, "openChests")

    def test_a_success_leaves_nothing_parked(self):
        orchestrator = make(api=FakeApi())
        inventory = self._chest_inventory()
        first = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), None, None, None, inventory)
        self.assertEqual(first.action, "openChests")
        self.assertFalse(orchestrator.rejections.parked_actions("w1"))

    def test_a_refused_level_reward_does_not_freeze_the_wallet(self):
        """Measured live on 2026-08-22: sixteen wallets sat at 40/40 slots with
        an unclaimed level-20 reward. claimInitialReward answered "Inventory
        full", which is benign, and the branch returned it again every cycle for
        thirteen hours — zero errors, zero actions, zero gold."""
        api = FakeApi()
        api.fail_with = ("claimInitialReward", "FAILED_PRECONDITION",
                         "Inventory full. Please clear some space.")
        orchestrator = make(api=api)
        state = parse_player({
            "level": 20, "grade": 2, "energy": 80, "maxEnergy": 100,
            "balance": 5000, "currentHealth": 210, "currentMana": 130,
            "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 20)), "newbieQuest": 999,
            "activity": None, "freeEnergyRefillsToday": 3,
            "lastFreeEnergyRefillDate": "2099-01-01"})

        first = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, None)
        self.assertEqual(first.action, "claimInitialReward")

        second = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, None)
        self.assertNotEqual(second.action, "claimInitialReward")

    def test_a_refused_gear_upgrade_does_not_freeze_the_wallet(self):
        """Same shape, different branch: the swap needs a free slot to put the
        worn piece into, so a full bag refuses it benignly and forever."""
        from slcw import inventory as inv
        api = FakeApi()
        api.fail_with = ("unequipItem", "FAILED_PRECONDITION",
                         "Your inventory is full. Clear some space first.")
        orchestrator = make(api=api)
        inventory = inv.parse_inventory({"maxSlots": 1, "slots": [
            {"slotIndex": 0, "templateId": "plate_helmet_t2",
             "quantity": 1, "instanceId": "i1"}]})
        state = parse_player({
            "level": 6, "grade": 2, "energy": 80, "maxEnergy": 100,
            "balance": 5000, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999,
            "activity": None, "freeEnergyRefillsToday": 3,
            "lastFreeEnergyRefillDate": "2099-01-01",
            "equipment": {"head": {"templateId": "plate_helmet_t1",
                                   "instanceId": "worn"}}})

        first = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, inventory)
        self.assertEqual(first.action, "upgradeEquip")

        second = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, inventory)
        self.assertNotEqual(second.action, "upgradeEquip")

    def test_an_open_battle_is_never_parked(self):
        """Parking resumeBattle would leave the wallet busy forever."""
        api = FakeApi()
        orchestrator = make(api=api)
        orchestrator.rejections.park(
            "w1", "resumeBattle", {"battleId": "b1", "monsterId": "m1"}, "nope")
        self.assertFalse(orchestrator.rejections.parked_actions("w1"))


@patch("slcw.orchestrator.time.sleep", lambda *_: None)
class DiscardBranchTests(unittest.TestCase):
    """The delete branch is last in line and silent unless asked for."""

    class Market:
        def __init__(self, fresh=True):
            self._fresh = fresh

        def is_fresh(self, _ttl):
            return self._fresh

        def best_bid(self, _item):
            return None

        def best_ask(self, _item):
            return None

    def _full_bag(self):
        from slcw import inventory as inv
        return inv.parse_inventory({"maxSlots": 2, "slots": [
            {"slotIndex": 0, "templateId": "frogslime",
             "quantity": 61, "instanceId": None},
            {"slotIndex": 1, "templateId": "spiderfang",
             "quantity": 4, "instanceId": None}]})

    def test_it_does_nothing_unless_the_fleet_asked_for_it(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False))
        self.assertFalse(orchestrator.config.discard_junk)
        chosen = orchestrator.build_candidates(
            state_of(), market=self.Market(), inventory=self._full_bag(),
            wallet_id="w1")
        self.assertNotIn("deleteInventoryItem", [c.action for c in chosen])

    def test_a_stale_market_destroys_nothing(self):
        """No fresh book means no proof the item is worthless."""
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, discard_junk=True))
        chosen = orchestrator.build_candidates(
            state_of(), market=self.Market(fresh=False),
            inventory=self._full_bag(), wallet_id="w1")
        self.assertNotIn("deleteInventoryItem", [c.action for c in chosen])

    def _chest_bag(self, max_slots):
        from slcw import inventory as inv
        return inv.parse_inventory({"maxSlots": max_slots, "slots": [
            {"slotIndex": 0, "templateId": "frogslime",
             "quantity": 61, "instanceId": None},
            {"slotIndex": 1, "templateId": "small_equip_chest",
             "quantity": 1, "instanceId": None}]})

    def test_a_chest_is_opened_rather_than_anything_destroyed(self):
        """One free slot is all it takes for the non-destructive route to win."""
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, discard_junk=True))
        chosen = orchestrator.build_candidates(
            state_of(), market=self.Market(), inventory=self._chest_bag(3),
            wallet_id="w1")
        self.assertEqual([c.action for c in chosen], ["openChests"])

    def test_junk_is_destroyed_to_make_room_for_the_chest_not_instead_of_it(self):
        """A chest pays out into a fresh slot, so a bag at 40/40 cannot open one
        at all. Destroying the junk stack is what gets the chest opened next
        cycle — and the chest itself is never a candidate for destruction."""
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, discard_junk=True))
        chosen = orchestrator.build_candidates(
            state_of(), market=self.Market(), inventory=self._chest_bag(2),
            wallet_id="w1")
        self.assertEqual([c.action for c in chosen], ["deleteInventoryItem"])
        self.assertEqual(chosen[0].params, {"slotIndex": 0})

    def test_it_fires_once_nothing_else_is_left(self):
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, discard_junk=True))
        chosen = orchestrator.build_candidates(
            state_of(), market=self.Market(), inventory=self._full_bag(),
            wallet_id="w1")
        self.assertEqual([c.action for c in chosen], ["deleteInventoryItem"])
        self.assertEqual(chosen[0].params, {"slotIndex": 1})


class ResolvedBattleTests(unittest.TestCase):
    """A battle the server has already decided still has to be settled.

    Measured live on 2026-08-21: wallet-13 held an open battle activity for nine
    and a half hours. processTurn answered FAILED_PRECONDITION "Battle is not
    active" — the fight was won, only the activity was never closed. That status
    is benign, so the exception left the turn loop before finishActivity, the
    reward stayed unclaimed, the activity stayed open, and the wallet read as
    busy on every later cycle while reporting zero errors.
    """

    def _api(self):
        api = FakeApi()
        api.fail_with = ("processTurn", "FAILED_PRECONDITION",
                         "Battle is not active")
        return api

    def test_a_finished_fight_is_settled_rather_than_re_fought(self):
        api = self._api()
        orchestrator = make(api=api)
        state = state_of(activity={"type": "battle", "startTime": {"seconds": 1},
                                   "data": {"battleId": "b1",
                                            "monsterId": "antimage_lvl10_3"}})
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "resumeBattle")
        self.assertEqual(decision.error, "")
        self.assertIn("finishActivity", [name for name, _ in api.calls])

    def test_a_real_failure_still_surfaces(self):
        """Only 'already resolved' is swallowed; a broken turn is still an error."""
        api = FakeApi()
        api.fail_with = ("processTurn", "INVALID_ARGUMENT",
                         "Invalid attack or defense zone")
        orchestrator = make(api=api)
        state = state_of(activity={"type": "battle", "startTime": {"seconds": 1},
                                   "data": {"battleId": "b1", "monsterId": "m1"}})
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertIn("INVALID_ARGUMENT", decision.error)

    def test_a_task_battle_is_settled_too(self):
        api = self._api()
        orchestrator = make(api=api)
        result = orchestrator.run_task_battle(None, "antimage_lvl10_3")
        self.assertIn("finishActivity", [name for name, _ in api.calls])
        self.assertTrue(result)


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


class ExecutorCoverageTests(unittest.TestCase):
    """Every action the engine can choose must have something that runs it.

    Found live on 2026-08-21: `generateClanQuest` was added as a candidate and
    the executor was never written, so wallet-01 chose it, raised
    "orchestrator has no executor", and — because the clan branch is ranked
    above the hunt chain — chose it again every cycle. The wallet did nothing
    else at all and was two errors from self-pausing. A candidate with no
    executor is not a missing feature, it is a wallet that stops playing.
    """

    import ast as _ast
    import pathlib as _pathlib

    ROOT = _pathlib.Path(__file__).resolve().parent.parent

    def _literals(self, path, func_names):
        """Action names passed to the candidate constructors in a module."""
        tree = self._ast.parse((self.ROOT / path).read_text())
        found = set()
        for node in self._ast.walk(tree):
            if not isinstance(node, self._ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in func_names:
                continue
            args = list(node.args)
            # `_free(wallet_id, action, ...)` wraps free_candidate with the
            # rejection check, so its action name sits one place further along.
            if name == "_free" and args:
                args.pop(0)
            for kw in node.keywords:
                if kw.arg == "action":
                    args.insert(0, kw.value)
            if args and isinstance(args[0], self._ast.Constant) \
                    and isinstance(args[0].value, str):
                found.add(args[0].value)
        return found

    def _executors(self):
        """Action names the execute() dispatch has a branch for."""
        tree = self._ast.parse((self.ROOT / "slcw/orchestrator.py").read_text())
        found = set()
        for node in self._ast.walk(tree):
            if not isinstance(node, self._ast.Compare):
                continue
            left = node.left
            if getattr(left, "id", None) != "action":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, self._ast.Constant) \
                        and isinstance(comparator.value, str):
                    found.add(comparator.value)
                elif isinstance(comparator, (self._ast.Tuple, self._ast.List,
                                             self._ast.Set)):
                    for element in comparator.elts:
                        if isinstance(element, self._ast.Constant) \
                                and isinstance(element.value, str):
                            found.add(element.value)
        return found

    def test_every_choosable_action_has_an_executor(self):
        from slcw import guardrails
        chosen = (self._literals("slcw/orchestrator.py", {"free_candidate", "_free"})
                  | self._literals("slcw/economy.py",
                                   {"ActionScore", "free_candidate"}))
        # Only real server calls need an executor; the rest are bookkeeping.
        chosen &= guardrails.ALLOWED_CALLABLES
        missing = sorted(chosen - self._executors())
        self.assertEqual(missing, [], f"no executor for: {missing}")

    def test_the_scan_actually_finds_the_known_actions(self):
        """A scan that silently matched nothing would pass the test above."""
        chosen = self._literals("slcw/orchestrator.py", {"free_candidate", "_free"})
        self.assertIn("createClan", chosen)
        self.assertIn("generateClanQuest", chosen)
        self.assertIn("createClan", self._executors())


class CaravanBranchTests(unittest.TestCase):
    """The level-20 trade branch: when it appears, and when it must not."""

    def trader(self, **overrides):
        """A wallet at the gate with its level rewards already claimed.

        Unclaimed level rewards and a pending grade-up are both free value and
        short-circuit the whole ranking, so a wallet still carrying either
        tests nothing about trading. Grade 2 at level 20 is mid-band: below the
        cap, above the caravan gate.
        """
        doc = {"level": 20, "grade": 2, "balance": 200_000, "energy": 90,
               "claimedInitialRewardsV2": list(range(1, 21))}
        doc.update(overrides)
        return state_of(**doc)

    def cities(self):
        """Virtan holding battle_ember, and Greyholm short of it."""
        return {
            "city_2": {
                "warehouseCapacity": 50_000, "taxRate": 10,
                "warehouse": {"isTradeHub": True, "input": [],
                              "outputs": [{"templateId": "battle_ember",
                                           "quantity": 40_000}]},
            },
            "city_17": {
                "warehouseCapacity": 5_000, "taxRate": 10,
                "warehouse": {"output": {"templateId": "chronicle_page",
                                         "quantity": 400},
                              "input": [{"templateId": "battle_ember",
                                         "quantity": 0}]},
            },
        }

    def trade_in(self, candidates):
        return next((c for c in candidates if c.action == "dispatchCaravan"), None)

    def test_a_wallet_below_the_gate_plays_exactly_as_before(self):
        orchestrator = make()
        state = self.trader(level=19)
        with_cities = orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities())
        without = orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w")
        self.assertIsNone(self.trade_in(with_cities))
        self.assertEqual([c.action for c in with_cities],
                         [c.action for c in without])

    def test_at_the_gate_a_load_is_offered(self):
        orchestrator = make()
        state = self.trader()
        trade = self.trade_in(orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities()))
        self.assertIsNotNone(trade)
        self.assertEqual(trade.params["destinationId"], "city_17")
        self.assertEqual(trade.params["templateId"], "battle_ember")
        self.assertEqual(trade.energy_cost, 20)

    def test_a_load_that_cannot_be_paid_for_is_not_offered(self):
        orchestrator = make()
        state = self.trader(balance=100)
        self.assertIsNone(self.trade_in(orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities())))

    def test_a_dispatch_needs_its_twenty_energy(self):
        orchestrator = make()
        state = self.trader(energy=19)
        self.assertIsNone(self.trade_in(orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities())))

    def test_the_load_shrinks_to_what_the_purse_holds(self):
        orchestrator = make()
        rich = self.trader()
        poor = self.trader(balance=20_000)
        full = self.trade_in(orchestrator.build_candidates(
            rich, build_snapshot([]), {}, wallet_id="w", cities=self.cities()))
        part = self.trade_in(orchestrator.build_candidates(
            poor, build_snapshot([]), {}, wallet_id="w", cities=self.cities()))
        self.assertEqual(full.params["quantity"], 10)
        self.assertLess(part.params["quantity"], 10)
        self.assertLessEqual(part.gold_cost, 20_000)

    def test_a_wallet_away_from_the_cities_is_offered_the_walk_not_the_load(self):
        orchestrator = make()
        state = self.trader(currentLocationId="farm_3")
        candidates = orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities())
        self.assertIsNone(self.trade_in(candidates))
        walk = next(c for c in candidates
                    if c.action == "startTravel" and "trade" in c.reason)
        self.assertEqual(walk.params["destinationId"], "city_2")

    def test_travel_to_trade_respects_the_auto_travel_switch(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False,
                                          auto_travel=False))
        state = self.trader(currentLocationId="farm_3")
        candidates = orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities())
        self.assertEqual([c for c in candidates if c.action == "startTravel"], [])

    def test_a_parked_route_is_not_offered_again(self):
        orchestrator = make()
        state = self.trader()
        trade = self.trade_in(orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities()))
        orchestrator.rejections.park("w", "dispatchCaravan", trade.params,
                                     "not enough stock")
        self.assertIsNone(self.trade_in(orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities())))

    def test_the_dispatch_carries_its_outlay_into_the_ledger(self):
        api = FakeApi()
        orchestrator = make(api=api)
        state = self.trader()
        trade = self.trade_in(orchestrator.build_candidates(
            state, build_snapshot([]), {}, wallet_id="w", cities=self.cities()))
        result = orchestrator.execute(None, trade, state)
        self.assertEqual(result["goldSpent"], trade.gold_cost)
