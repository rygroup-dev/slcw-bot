"""Attribute derivation and allocation policy.

Formulas taken from the profile page, and cross-checked against a real battle
document: a level-6 character with all attributes at 3 reported maxHP 130,
weaponPower 6, spellPower 6, precision 6, impact 6.
"""
import unittest

from slcw import build
from slcw.config import Config
from slcw.model import parse_player
from tests.test_orchestrator import FakeApi, make

BASE = {"vitality": 3, "might": 3, "dexterity": 3, "wisdom": 3, "intelligence": 3}


class DerivationTests(unittest.TestCase):
    def test_matches_the_observed_battle_document(self):
        """A live battle reported these exact values for an all-3 character."""
        stats = build.derive(BASE)
        self.assertEqual(stats.weapon_power, 6)
        self.assertEqual(stats.spell_power, 6)
        self.assertEqual(stats.precision, 6)
        self.assertEqual(stats.impact, 6)
        self.assertEqual(stats.max_health, 130)
        self.assertEqual(stats.max_mana, 130)

    def test_physical_defense_formula(self):
        # 2*vit + might + 1.5*dex
        self.assertAlmostEqual(build.derive(BASE).physical_defense, 6 + 3 + 4.5)

    def test_magical_defense_formula(self):
        # vit + 1.5*dex + 2*wis
        self.assertAlmostEqual(build.derive(BASE).magical_defense, 3 + 4.5 + 6)

    def test_vitality_is_the_only_source_of_health(self):
        low = build.derive(dict(BASE, vitality=3)).max_health
        high = build.derive(dict(BASE, vitality=13)).max_health
        self.assertEqual(high - low, 100)
        self.assertEqual(build.derive(dict(BASE, might=99)).max_health, low)

    def test_might_drives_weapon_power(self):
        self.assertEqual(build.derive(dict(BASE, might=10)).weapon_power, 20)

    def test_dexterity_feeds_four_stats_at_once(self):
        base = build.derive(BASE)
        dexy = build.derive(dict(BASE, dexterity=13))
        self.assertGreater(dexy.precision, base.precision)
        self.assertGreater(dexy.impact, base.impact)
        self.assertGreater(dexy.physical_defense, base.physical_defense)
        self.assertGreater(dexy.magical_defense, base.magical_defense)

    def test_missing_attributes_default_to_three(self):
        self.assertEqual(build.derive({}).max_health, 130)


class EquipmentTests(unittest.TestCase):
    def test_flat_stat_bonus_is_added(self):
        equipment = {"head": {"templateId": "plate_helmet_t1",
                              "stats": {"statType": "physicalDefense", "baseValue": 4}}}
        stats = build.derive(BASE, equipment)
        self.assertAlmostEqual(stats.physical_defense, 6 + 3 + 4.5 + 4)

    def test_attribute_bonus_from_a_list(self):
        equipment = {"chest": {"templateId": "plate_armor_t1",
                               "bonusStats": [{"attribute": "might", "value": 5}]}}
        self.assertEqual(build.derive(BASE, equipment).weapon_power, 2 * 8)

    def test_attribute_bonus_from_a_map(self):
        equipment = {"chest": {"templateId": "plate_armor_t1",
                               "bonusStats": {"might": 5}}}
        self.assertEqual(build.derive(BASE, equipment).weapon_power, 2 * 8)

    def test_equipment_attributes_do_not_raise_health(self):
        """Health follows the raw attribute, not the gear-boosted total."""
        equipment = {"chest": {"templateId": "plate_armor_t1",
                               "bonusStats": {"vitality": 50}}}
        self.assertEqual(build.derive(BASE, equipment).max_health, 130)

    def test_plate_set_bonus_multiplies_physical_defense(self):
        equipment = {slot: {"templateId": f"plate_{slot}_t1"}
                     for slot in ("helmet", "armor", "boots", "greaves", "gauntlets")}
        plain = build.derive(BASE).physical_defense
        setted = build.derive(BASE, equipment)
        self.assertAlmostEqual(setted.physical_defense, plain * 1.2)
        self.assertIn("plate", setted.set_bonus)

    def test_cloth_set_lifts_magic_defence_and_spell_power(self):
        equipment = {slot: {"templateId": f"cloth_{slot}_t2"}
                     for slot in ("helmet", "armor", "boots", "greaves", "gauntlets")}
        stats = build.derive(BASE, equipment)
        self.assertAlmostEqual(stats.spell_power, 6 * 1.2)
        self.assertIn("cloth", stats.set_bonus)

    def test_magic_items_count_as_cloth(self):
        equipment = {slot: {"templateId": f"magic_{slot}_t1"}
                     for slot in ("helmet", "armor", "boots", "greaves", "gauntlets")}
        self.assertIn("cloth", build.derive(BASE, equipment).set_bonus)

    def test_four_pieces_earn_no_set_bonus(self):
        equipment = {slot: {"templateId": f"plate_{slot}_t1"}
                     for slot in ("helmet", "armor", "boots", "greaves")}
        self.assertEqual(build.derive(BASE, equipment).set_bonus, "")

    def test_mixed_tiers_do_not_form_a_set(self):
        equipment = {f"s{i}": {"templateId": f"plate_armor_t{i}"} for i in range(1, 6)}
        self.assertEqual(build.derive(BASE, equipment).set_bonus, "")

    def test_empty_equipment_is_safe(self):
        self.assertEqual(build.derive(BASE, {}).set_bonus, "")
        self.assertEqual(build.derive(BASE, None).max_health, 130)


class AllocationTests(unittest.TestCase):
    def test_named_builds_exist(self):
        self.assertEqual(set(build.BUILDS), {"sustain", "balanced", "damage"})

    def test_every_build_explains_itself(self):
        for policy in build.BUILDS.values():
            self.assertTrue(policy.summary.strip())

    def test_sustain_leads_with_vitality(self):
        """Vitality is the only attribute that raises max health."""
        chosen = build.next_attribute(BASE, "sustain")
        self.assertEqual(chosen, "vitality")

    def test_damage_leads_with_might(self):
        self.assertEqual(build.next_attribute(BASE, "damage"), "might")

    def test_allocation_converges_on_the_target_ratio(self):
        attributes = dict(BASE)
        for _ in range(120):
            attributes[build.next_attribute(attributes, "sustain")] += 1

        vitality = attributes["vitality"] - 3
        dexterity = attributes["dexterity"] - 3
        might = attributes["might"] - 3
        # Target is 3 : 2 : 1.
        self.assertAlmostEqual(vitality / dexterity, 1.5, delta=0.25)
        self.assertAlmostEqual(dexterity / might, 2.0, delta=0.4)

    def test_no_points_go_to_stats_the_build_excludes(self):
        """Physical monsters make spell power and mana worthless here."""
        attributes = dict(BASE)
        for _ in range(60):
            attributes[build.next_attribute(attributes, "sustain")] += 1
        self.assertEqual(attributes["intelligence"], 3)
        self.assertEqual(attributes["wisdom"], 3)

    def test_allocation_is_incremental_not_all_in_one_stat(self):
        attributes = dict(BASE)
        picks = set()
        for _ in range(12):
            chosen = build.next_attribute(attributes, "sustain")
            picks.add(chosen)
            attributes[chosen] += 1
        self.assertEqual(picks, {"vitality", "dexterity", "might"})

    def test_unknown_build_falls_back_to_the_default(self):
        self.assertEqual(build.get_build("nonsense").name, build.DEFAULT_BUILD)
        self.assertEqual(build.get_build("").name, build.DEFAULT_BUILD)

    def test_sustain_really_does_out_heal_damage_build(self):
        """The policy names should match what they actually produce."""
        sustain = dict(BASE)
        damage = dict(BASE)
        for _ in range(60):
            sustain[build.next_attribute(sustain, "sustain")] += 1
            damage[build.next_attribute(damage, "damage")] += 1
        self.assertGreater(build.derive(sustain).max_health,
                           build.derive(damage).max_health)
        self.assertGreater(build.derive(damage).weapon_power,
                           build.derive(sustain).weapon_power)


class OrchestratorTests(unittest.TestCase):
    def _state(self, points=2, **attrs):
        merged = dict(BASE)
        merged.update(attrs)
        return parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 100,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_2",
            "attributePoints": points, "attributes": merged,
            "claimedInitialRewardsV2": list(range(1, 7)), "activity": None})

    def test_points_are_spent_by_the_configured_build(self):
        api = FakeApi()
        orchestrator = make(config=Config(enabled=True, dry_run=False, build="damage"),
                            api=api)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, self._state())
        self.assertEqual(decision.action, "spendAttributePoints")
        self.assertEqual(decision.params["targetId"], "might")
        self.assertIn("damage build", decision.reason)

    def test_default_build_is_sustain(self):
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        decision = orchestrator.decide_and_act({"id": "w1"}, None, self._state())
        self.assertEqual(decision.params["targetId"], "vitality")

    def test_nothing_spent_when_no_points_are_available(self):
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        candidates = orchestrator.build_candidates(self._state(points=0))
        self.assertNotIn("spendAttributePoints", [c.action for c in candidates])


if __name__ == "__main__":
    unittest.main()


class LevelingTests(unittest.TestCase):
    """Levelling is a manual click; an unattended account simply stops."""

    def test_xp_curve_matches_the_bundle_table(self):
        from slcw import leveling
        self.assertEqual(leveling.xp_required(1), 50)
        self.assertEqual(leveling.xp_required(2), 100)
        self.assertEqual(leveling.xp_required(10), 1_000)
        self.assertEqual(leveling.xp_required(20), 5_000)

    def test_curve_is_monotonic(self):
        from slcw import leveling
        values = [leveling.xp_required(l) for l in range(1, 87)]
        self.assertEqual(values, sorted(values))

    def test_below_and_beyond_the_table(self):
        from slcw import leveling
        self.assertEqual(leveling.xp_required(0), 50)
        self.assertEqual(leveling.xp_required(999), leveling.XP_BEYOND_TABLE)

    def test_grade_caps_the_level(self):
        from slcw import leveling
        self.assertEqual(leveling.level_cap(1), 15)
        self.assertEqual(leveling.level_cap(4), 60)
        self.assertTrue(leveling.at_grade_cap(15, 1))
        self.assertFalse(leveling.at_grade_cap(14, 1))

    def test_level_up_requires_enough_xp(self):
        from slcw import leveling
        self.assertFalse(leveling.can_level_up(1, 1, xp=49))
        self.assertTrue(leveling.can_level_up(1, 1, xp=50))

    def test_grade_cap_blocks_even_with_ample_xp(self):
        from slcw import leveling
        self.assertFalse(leveling.can_level_up(15, 1, xp=10**9))

    def test_blocked_reason_explains_itself(self):
        from slcw import leveling
        self.assertIn("more XP", leveling.blocked_reason(1, 1, 10))
        self.assertIn("grade cap", leveling.blocked_reason(15, 1, 10**9))
        self.assertEqual(leveling.blocked_reason(1, 1, 50), "")

    def test_payload_never_selects_the_diamond_booster(self):
        """The diamond branch charges 99*(level+1); it must be unreachable."""
        from slcw import leveling
        self.assertEqual(leveling.payload()["booster"], "none")
        self.assertEqual(leveling.payload(3)["cardIndex"], 3)

    def test_progress_is_capped(self):
        from slcw import leveling
        self.assertAlmostEqual(leveling.progress(1, 25), 0.5)
        self.assertEqual(leveling.progress(1, 10**6), 1.0)

    def test_orchestrator_levels_up_when_it_can(self):
        api = FakeApi()
        api.buy_level = lambda s, p: api._record("buyLevel", **p)
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        state = parse_player({
            "level": 1, "grade": 1, "xp": 500, "energy": 80, "maxEnergy": 100,
            "balance": 100, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_2", "attributes": dict(BASE),
            "claimedInitialRewardsV2": [1], "activity": None})
        decision = orchestrator.decide_and_act({"id": "w1"}, None, state)
        self.assertEqual(decision.action, "buyLevel")
        self.assertEqual(decision.params["booster"], "none")

    def test_no_level_up_at_the_grade_cap(self):
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        state = parse_player({
            "level": 15, "grade": 1, "xp": 10**6, "energy": 80, "maxEnergy": 100,
            "balance": 100, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_2", "attributes": dict(BASE),
            "claimedInitialRewardsV2": list(range(1, 16)), "activity": None})
        candidates = orchestrator.build_candidates(state)
        self.assertNotIn("buyLevel", [c.action for c in candidates])
