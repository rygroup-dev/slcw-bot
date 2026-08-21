import unittest

from slcw import crafting
from slcw import inventory as inv
from slcw.config import Config
from slcw.model import parse_player
from tests.test_orchestrator import FakeApi, make


def document(*slots, max_slots=20):
    return {"maxSlots": max_slots, "slots": [
        {"slotIndex": i, "templateId": t, "quantity": q, "instanceId": iid}
        for i, (t, q, iid) in enumerate(slots)]}


def chest_state():
    return parse_player({
        "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
        "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
        "attributes": {"vitality": 3, "wisdom": 3},
        "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999,
        "activity": None, "freeEnergyRefillsToday": 3,
        "lastFreeEnergyRefillDate": "2099-01-01"})


class InventoryParsingTests(unittest.TestCase):
    def test_parses_the_live_slot_shape(self):
        parsed = inv.parse_inventory(document(
            ("spiderfang", 1, None), ("small_equip_chest", 1, None), (None, 0, None)))
        self.assertEqual(parsed.max_slots, 20)
        self.assertEqual(parsed.used_slots, 2)
        self.assertEqual(parsed.free_slots, 18)

    def test_holdings_aggregate_across_slots(self):
        parsed = inv.parse_inventory(document(
            ("copper_ore", 40, None), ("copper_ore", 60, None)))
        self.assertEqual(parsed.holdings()["copper_ore"], 100)

    def test_empty_slots_are_ignored(self):
        parsed = inv.parse_inventory(document((None, 0, None), ("", 0, None)))
        self.assertEqual(parsed.holdings(), {})
        self.assertEqual(parsed.used_slots, 0)

    def test_nearly_full_is_flagged(self):
        full = inv.parse_inventory(document(
            *[("copper_ore", 1, None)] * 9, max_slots=10))
        self.assertTrue(full.is_nearly_full)

    def test_missing_document_does_not_crash(self):
        self.assertEqual(inv.parse_inventory(None).slots, [])
        self.assertEqual(inv.parse_inventory({}).holdings(), {})


class ChestTests(unittest.TestCase):
    def test_finds_openable_containers(self):
        parsed = inv.parse_inventory(document(
            ("copper_ore", 99, None), ("small_equip_chest", 2, None),
            ("large_weapon_chest", 5, None)))
        chests = parsed.chests()
        self.assertEqual([c.template_id for c in chests],
                         ["large_weapon_chest", "small_equip_chest"])

    def test_materials_are_not_chests(self):
        parsed = inv.parse_inventory(document(("copper_ingot", 50, None)))
        self.assertEqual(parsed.chests(), [])

    def test_orchestrator_opens_chests_before_anything_else(self):
        api = FakeApi()
        opened = {}
        api.open_chests = lambda s, t, q: opened.update(t=t, q=q) or {"rewards": []}
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        state = parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})
        parsed = inv.parse_inventory(document(("small_equip_chest", 3, None)))
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, parsed)
        self.assertEqual(decision.action, "openChests")
        self.assertEqual(opened["t"], "small_equip_chest")
        self.assertEqual(opened["q"], 3)

    def test_batch_is_capped(self):
        from slcw.orchestrator import MAX_CHESTS_PER_OPEN
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        state = parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})
        parsed = inv.parse_inventory(document(("small_equip_chest", 999, None)))
        candidates = orchestrator.build_candidates(state, inventory=parsed)
        self.assertEqual(candidates[0].params["quantity"], MAX_CHESTS_PER_OPEN)


    def test_a_full_inventory_does_not_open_chests(self):
        """Measured live: openChests answers FAILED_PRECONDITION "Not enough
        space in inventory", which is benign — so the wallet retries forever."""
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        parsed = inv.parse_inventory(document(
            *[("copper_ore", 1, None)] * 9, ("small_equip_chest", 4, None),
            max_slots=10))
        candidates = orchestrator.build_candidates(
            chest_state(), inventory=parsed, wallet_id="w1")
        self.assertNotEqual(
            [c.action for c in candidates][:1], ["openChests"])

    def test_the_batch_never_exceeds_the_free_slots(self):
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        parsed = inv.parse_inventory(document(
            *[("copper_ore", 1, None)] * 7, ("small_equip_chest", 9, None),
            max_slots=10))
        candidates = orchestrator.build_candidates(
            chest_state(), inventory=parsed, wallet_id="w1")
        self.assertEqual(candidates[0].action, "openChests")
        self.assertEqual(candidates[0].params["quantity"], 2)


class EquipTests(unittest.TestCase):
    def test_item_slots_are_known(self):
        self.assertEqual(inv.slot_of("two_handed_sword_t1"), "two_hand_weapon")
        self.assertEqual(inv.slot_of("plate_helmet_t1"), "head")
        self.assertIsNone(inv.slot_of("copper_ore"))

    def test_tier_parsed_from_the_id(self):
        self.assertEqual(inv.tier_of("plate_helmet_t4"), 4)
        self.assertEqual(inv.tier_of("copper_ore"), 0)

    def test_empty_slot_is_free(self):
        self.assertTrue(inv.slot_is_free({}, "head"))

    def test_occupied_slot_is_not_free(self):
        self.assertFalse(inv.slot_is_free(
            {"head": {"templateId": "plate_helmet_t1"}}, "head"))

    def test_two_handed_conflicts_with_one_handed(self):
        """A worn one-hander blocks the two-hand slot even though it reads empty."""
        worn = {"right_weapon": {"templateId": "one_handed_sword_t1"}}
        self.assertFalse(inv.slot_is_free(worn, "two_hand_weapon"))

    def test_one_handed_blocked_by_a_worn_two_hander(self):
        worn = {"two_hand_weapon": {"templateId": "two_handed_sword_t1"}}
        self.assertFalse(inv.slot_is_free(worn, "left_weapon"))

    def test_equips_into_an_empty_slot(self):
        parsed = inv.parse_inventory(document(("plate_helmet_t1", 1, "inst-1")))
        action = inv.next_equip(parsed, {})
        self.assertEqual(action.instance_id, "inst-1")
        self.assertEqual(action.slot, "head")
        self.assertFalse(action.is_upgrade)

    def test_ignores_a_worse_piece_than_the_one_worn(self):
        parsed = inv.parse_inventory(document(("plate_helmet_t1", 1, "inst-1")))
        worn = {"head": {"templateId": "plate_helmet_t3"}}
        self.assertIsNone(inv.next_equip(parsed, worn))

    def test_identifies_a_strict_upgrade(self):
        parsed = inv.parse_inventory(document(("plate_helmet_t5", 1, "inst-9")))
        worn = {"head": {"templateId": "plate_helmet_t2"}}
        action = inv.next_equip(parsed, worn)
        self.assertTrue(action.is_upgrade)
        self.assertEqual(action.replaces_tier, 2)

    def test_free_slots_are_preferred_over_swaps(self):
        parsed = inv.parse_inventory(
            document(("plate_helmet_t5", 1, "up"), ("plate_boots_t1", 1, "free")))
        worn = {"head": {"templateId": "plate_helmet_t1"}}
        self.assertEqual(inv.next_equip(parsed, worn).instance_id, "free")

    def test_gear_above_the_players_grade_is_skipped(self):
        """Measured live: equipItem on a t2 piece at grade 1 answers
        FAILED_PRECONDITION "Your grade (1) is too low for this item (Grade 2)".
        That reads as benign, so proposing it loops the wallet forever."""
        parsed = inv.parse_inventory(document(("plate_boots_t2", 1, "inst-2")))
        self.assertIsNone(inv.next_equip(parsed, {}, grade=1))

    def test_gear_at_the_players_grade_is_worn(self):
        parsed = inv.parse_inventory(document(("plate_boots_t2", 1, "inst-2")))
        self.assertEqual(inv.next_equip(parsed, {}, grade=2).instance_id, "inst-2")

    def test_a_wearable_piece_is_taken_when_a_better_one_is_locked(self):
        parsed = inv.parse_inventory(document(
            ("plate_boots_t2", 1, "locked"), ("plate_helmet_t1", 1, "wearable")))
        self.assertEqual(
            inv.next_equip(parsed, {}, grade=1).instance_id, "wearable")

    def test_an_upgrade_above_the_grade_is_skipped_too(self):
        parsed = inv.parse_inventory(document(("plate_helmet_t2", 1, "up")))
        worn = {"head": {"templateId": "plate_helmet_t1"}}
        self.assertIsNone(inv.next_equip(parsed, worn, grade=1))

    def test_stackable_material_is_never_equipped(self):
        parsed = inv.parse_inventory(document(("copper_ore", 99, None)))
        self.assertIsNone(inv.next_equip(parsed, {}))

    def test_orchestrator_equips_into_an_empty_slot(self):
        api = FakeApi()
        equipped = {}
        api.equip_item = lambda s, i: equipped.update(id=i) or {"success": True}
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        state = parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3}, "equipment": {},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})
        parsed = inv.parse_inventory(document(("plate_helmet_t1", 1, "inst-1")))
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, parsed)
        self.assertEqual(decision.action, "equipItem")
        self.assertEqual(equipped["id"], "inst-1")

    def test_swap_is_offered_as_a_single_upgrade_action(self):
        """A strictly higher tier in an occupied slot is still a pure gain."""
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        state = parse_player({
            "level": 6, "grade": 5, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "equipment": {slot: {"templateId": f"plate_helmet_t1"}
                          for slot in ("head", "chest", "gauntlets", "greaves",
                                       "boots", "two_hand_weapon")},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})
        parsed = inv.parse_inventory(document(("plate_helmet_t5", 1, "up")))
        candidates = orchestrator.build_candidates(state, inventory=parsed)
        self.assertEqual(candidates[0].action, "upgradeEquip")
        self.assertEqual(candidates[0].params, {"slot": "head", "instanceId": "up"})

    def test_orchestrator_unequips_before_equipping_an_upgrade(self):
        api = FakeApi()
        calls = []
        api.unequip_item = lambda s, slot: calls.append(("unequip", slot)) or {"success": True}
        api.equip_item = lambda s, i: calls.append(("equip", i)) or {"success": True}
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        state = parse_player({
            "level": 6, "grade": 5, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
            "attributes": {"vitality": 3, "wisdom": 3},
            "equipment": {"head": {"templateId": "plate_helmet_t1"}},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})
        parsed = inv.parse_inventory(document(("plate_helmet_t5", 1, "up")))
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, None, None, None, parsed)
        self.assertEqual(decision.action, "upgradeEquip")
        self.assertEqual(calls, [("unequip", "head"), ("equip", "up")])


class CraftingTests(unittest.TestCase):
    def test_catalog_loaded_completely(self):
        from slcw.crafting_data import CRAFT_RECIPES
        self.assertEqual(len(CRAFT_RECIPES), 154)
        self.assertEqual(set(crafting.WORKSHOPS),
                         {"thunder_forge", "silent_step", "arcane_sanctum"})

    def test_cost_and_duration_follow_the_client_formulas(self):
        self.assertEqual(crafting.gold_cost(1), 100)
        self.assertEqual(crafting.gold_cost(3), 900)
        self.assertEqual(crafting.duration_seconds(1), 60)
        self.assertEqual(crafting.duration_seconds(4), 480)

    def test_profession_gate_scales_by_fifteen(self):
        self.assertEqual(crafting.required_profession_level(1), 0)
        self.assertEqual(crafting.required_profession_level(4), 45)

    def test_tier_read_from_the_recipe_id(self):
        self.assertEqual(crafting.tier_of("two_handed_sword_t3"), 3)
        self.assertEqual(crafting.tier_of("mystery"), 1)

    def test_ingredients_scale_with_quantity(self):
        self.assertEqual(crafting.ingredients_for("two_handed_sword_t1", 2),
                         {"copper_ingot": 32})

    def test_blockers_name_every_obstacle(self):
        plan = crafting.CraftPlan(
            crafting.WORKSHOPS["thunder_forge"], "two_handed_sword_t3", 1)
        blockers = plan.blockers(holdings={}, gold=0, grade=1, professions={})
        joined = " ".join(blockers)
        self.assertIn("grade 1 below tier 3", joined)
        self.assertIn("armorsmith", joined)
        self.assertIn("steel_ingot", joined)

    def test_no_blockers_when_everything_is_in_place(self):
        plan = crafting.CraftPlan(
            crafting.WORKSHOPS["thunder_forge"], "two_handed_sword_t1", 1)
        self.assertEqual(plan.blockers({"copper_ingot": 16}, 1000, 1, {}), [])

    def test_max_quantity_limited_by_ingredients(self):
        self.assertEqual(crafting.max_quantity(
            "two_handed_sword_t1", {"copper_ingot": 40}, gold=10**6), 2)

    def test_max_quantity_limited_by_gold(self):
        self.assertEqual(crafting.max_quantity(
            "two_handed_sword_t1", {"copper_ingot": 999}, gold=350), 3)

    def test_craftable_lists_only_ready_recipes(self):
        plans = crafting.craftable(
            "city_1", {"copper_ingot": 16}, gold=1000, grade=1, professions={})
        self.assertTrue(plans)
        for plan in plans:
            self.assertEqual(plan.tier, 1)

    def test_nothing_craftable_without_materials(self):
        self.assertEqual(crafting.craftable("city_1", {}, 10**6, 7, {}), [])

    def test_payload_matches_the_frontend_shape(self):
        plan = crafting.CraftPlan(
            crafting.WORKSHOPS["thunder_forge"], "two_handed_sword_t1", 3)
        self.assertEqual(plan.payload(), {
            "workshopId": "thunder_forge", "recipeId": "two_handed_sword_t1",
            "quantity": 3})

    def test_crafting_is_never_chosen_automatically(self):
        """Equipment has no market bid, so its value cannot be measured."""
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        state = parse_player({
            "level": 60, "grade": 7, "energy": 100, "maxEnergy": 100,
            "balance": 10**7, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_1", "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 61)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})
        candidates = orchestrator.build_candidates(
            state, None, {"copper_ingot": 999}, inventory=None)
        self.assertNotIn("startCrafting", [c.action for c in candidates])

    def test_recipes_reference_only_known_materials(self):
        """Every ingredient should be something the chain can actually produce."""
        from slcw import refining
        from slcw.crafting_data import CRAFT_RECIPES
        from slcw.item_data import ITEM_SLOTS
        refined = {item for w in refining.WORKSHOPS.values() for item in w.items}
        for recipe_id, ingredients in CRAFT_RECIPES.items():
            for item, quantity in ingredients:
                self.assertGreater(quantity, 0, f"{recipe_id} needs {item}")
                known = item in refined or item in ITEM_SLOTS or item.startswith(
                    ("echo_", "apex_"))
                self.assertTrue(known, f"{recipe_id} needs unknown item {item}")


if __name__ == "__main__":
    unittest.main()
