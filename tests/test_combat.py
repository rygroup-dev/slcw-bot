import random
import tempfile
import unittest
from pathlib import Path

from slcw.combat import (ZONES, CombatMemory, MonsterModel, monster_level,
                         select_monster)


def turn(player_zone, player_type, monster_zone, monster_type="hit"):
    """Build a turnResult in the shape the server actually returns."""
    return {
        "round": 1,
        "player": {"zone": player_zone, "damage": 3, "type": player_type, "effects": []},
        "monster": {"zone": monster_zone, "damage": 2, "type": monster_type, "effects": []},
    }


class MonsterModelTests(unittest.TestCase):
    def test_learns_which_zone_the_monster_blocks(self):
        model = MonsterModel()
        for _ in range(20):
            model.observe(turn("head", "blocked", "torso"))
            model.observe(turn("legs", "hit", "torso"))
        self.assertGreater(model.block_rate("head"), model.block_rate("legs"))
        self.assertEqual(model.best_attack_zone(), "legs")

    def test_learns_which_zone_the_monster_attacks(self):
        model = MonsterModel()
        for _ in range(20):
            model.observe(turn("head", "hit", "torso"))
        self.assertEqual(model.best_defense_zone(), "torso")

    def test_priors_keep_rates_away_from_certainty(self):
        model = MonsterModel()
        model.observe(turn("head", "blocked", "torso"))
        self.assertLess(model.block_rate("head"), 1.0)
        self.assertGreater(model.block_rate("head"), 0.0)

    def test_unobserved_zones_stay_neutral(self):
        model = MonsterModel()
        self.assertAlmostEqual(model.block_rate("head"), model.block_rate("legs"))

    def test_roundtrip_serialisation(self):
        model = MonsterModel()
        model.observe(turn("head", "blocked", "legs"))
        restored = MonsterModel.from_dict(model.to_dict())
        self.assertEqual(restored.to_dict(), model.to_dict())


class CombatMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = CombatMemory(path=Path(self.tmp.name) / "combat.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_zone_choice_exploits_learned_model(self):
        for _ in range(60):
            self.memory.observe("spider", turn("head", "blocked", "torso"))
            self.memory.observe("spider", turn("legs", "hit", "torso"))

        # Seed chosen so no exploration roll fires; exercises the exploit path.
        rng = random.Random(7)
        attacks, defenses = [], []
        for _ in range(200):
            attack, defense = self.memory.choose_zones("spider", rng)
            attacks.append(attack)
            defenses.append(defense)

        self.assertGreater(attacks.count("legs"), attacks.count("head"))
        self.assertGreater(defenses.count("torso"), defenses.count("head"))

    def test_still_explores_other_zones(self):
        for _ in range(60):
            self.memory.observe("spider", turn("head", "blocked", "torso"))
        rng = random.Random(1)
        seen = {self.memory.choose_zones("spider", rng)[0] for _ in range(400)}
        self.assertEqual(seen, set(ZONES), "exploration must keep all zones reachable")

    def test_persists_across_instances(self):
        self.memory.observe("spider", turn("head", "blocked", "torso"))
        self.memory.save()
        reloaded = CombatMemory(path=self.memory.path)
        self.assertEqual(reloaded.model_for("spider").rounds, 1)

    def test_separate_models_per_monster(self):
        self.memory.observe("spider", turn("head", "blocked", "torso"))
        self.assertEqual(self.memory.model_for("aerial").rounds, 0)


class MonsterSelectionTests(unittest.TestCase):
    CATALOG = ["forestspider_lvl1_1", "forestspider_lvl1_2", "aerial_lvl4_1"]

    def test_parses_level_from_id(self):
        self.assertEqual(monster_level("forestspider_lvl1_2"), 1)
        self.assertEqual(monster_level("aerial_lvl4_1"), 4)
        self.assertEqual(monster_level("mystery_monster"), 1)

    def test_healthy_player_takes_the_hardest_eligible_monster(self):
        self.assertEqual(select_monster(self.CATALOG, 6, 1.0), "aerial_lvl4_1")

    def test_hurt_player_drops_to_an_easier_monster(self):
        chosen = select_monster(self.CATALOG, 4, 0.5)
        self.assertEqual(monster_level(chosen), 1)

    def test_low_level_player_never_picks_above_its_level(self):
        chosen = select_monster(self.CATALOG, 2, 1.0)
        self.assertLessEqual(monster_level(chosen), 2)

    def test_empty_catalog_returns_none(self):
        self.assertIsNone(select_monster([], 5, 1.0))



class MonsterRegistryTests(unittest.TestCase):
    """The registry is generated from the bundle; startBattle rejects invented ids."""

    def test_registry_is_populated(self):
        from slcw.monster_data import MONSTERS
        self.assertEqual(len(MONSTERS), 253)

    def test_previously_hardcoded_id_never_existed(self):
        from slcw.monster_data import MONSTERS
        self.assertNotIn("forestspider_lvl1_1", MONSTERS,
                         "the old catalog invented this id")
        self.assertIn("forestspider_lvl1_2", MONSTERS)
        self.assertIn("bigfrog_lvl1_1", MONSTERS)

    def test_every_id_matches_the_level_in_its_name(self):
        from slcw.combat import monster_level
        from slcw.monster_data import MONSTERS
        for monster_id, entry in MONSTERS.items():
            suffix = [c for c in monster_id.split("_") if c.startswith("lvl")]
            if suffix:
                self.assertEqual(int(suffix[0][3:]), entry[1], monster_id)
            self.assertEqual(monster_level(monster_id), entry[1])

    def test_known_monsters_can_be_capped_by_level(self):
        from slcw.combat import known_monsters, monster_level
        for monster_id in known_monsters(max_level=3):
            self.assertLessEqual(monster_level(monster_id), 3)

    def test_selection_defaults_to_the_registry(self):
        from slcw.combat import monster_level
        chosen = select_monster(None, player_level=5, health_ratio=1.0)
        self.assertIsNotNone(chosen)
        self.assertLessEqual(monster_level(chosen), 5)

    def test_selection_prefers_the_safer_monster_at_equal_level(self):
        from slcw.combat import monster_power
        chosen = select_monster(None, player_level=1, health_ratio=1.0)
        # Both level-1 monsters have power 3, so either is acceptable, but the
        # chosen one must never be the higher-powered of an equal-level pair.
        peers = [m for m in ("bigfrog_lvl1_1", "forestspider_lvl1_2")]
        self.assertIn(chosen, peers)
        self.assertLessEqual(monster_power(chosen),
                             max(monster_power(m) for m in peers))

    def test_hurt_player_drops_below_its_level(self):
        from slcw.combat import monster_level
        healthy = select_monster(None, player_level=10, health_ratio=1.0)
        hurt = select_monster(None, player_level=10, health_ratio=0.5)
        self.assertEqual(monster_level(healthy), 10)
        self.assertLessEqual(monster_level(hurt), 8)

    def test_names_resolve_from_the_registry(self):
        from slcw.combat import monster_health, monster_name
        self.assertEqual(monster_name("forestspider_lvl1_2"),
                         "Young Venomous Forest Spider")
        self.assertEqual(monster_health("forestspider_lvl1_2"), 32)
        self.assertEqual(monster_name("not_a_monster"), "not_a_monster")

if __name__ == "__main__":
    unittest.main()


class SurvivabilityTests(unittest.TestCase):
    """Level is a gate; whether we survive depends on actual combat stats."""

    def test_a_weak_character_refuses_a_tanky_monster(self):
        from slcw.combat import survivable
        # aerial_lvl4_1 against a starting character.
        self.assertFalse(survivable("aerial_lvl4_1", weapon_power=6,
                                    physical_defense=18, current_health=30))

    def test_a_strong_character_accepts_it(self):
        from slcw.combat import survivable
        self.assertTrue(survivable("aerial_lvl4_1", weapon_power=200,
                                   physical_defense=200, current_health=1000))

    def test_more_health_makes_a_fight_acceptable(self):
        from slcw.combat import survivable
        weak = survivable("spider_lvl2_1", 6, 18, current_health=20)
        strong = survivable("spider_lvl2_1", 6, 18, current_health=500)
        self.assertFalse(weak)
        self.assertTrue(strong)

    def test_unknown_monster_is_not_blocked(self):
        from slcw.combat import survivable
        self.assertTrue(survivable("mystery", 1, 1, 1))

    def test_selection_prefers_monsters_it_can_survive(self):
        from slcw.combat import survivable
        chosen = select_monster(
            None, player_level=10, health_ratio=1.0,
            weapon_power=60, physical_defense=40, current_health=200)
        self.assertIsNotNone(chosen)
        self.assertTrue(survivable(chosen, 60, 40, 200))

    def test_a_weak_character_still_gets_a_fight_it_can_reach(self):
        """Survivability is a preference, not a veto. It reads pessimistic —
        the fleet's level-15 wallets have won every fight it would refuse — and
        with a reach floor there is no harmless level-1 monster to retreat to,
        so vetoing here would stop a wallet fighting at all."""
        from slcw.combat import in_reach
        chosen = select_monster(
            None, player_level=10, health_ratio=1.0,
            weapon_power=6, physical_defense=18, current_health=25)
        self.assertIsNotNone(chosen)
        self.assertTrue(in_reach(chosen, 10))


class MeasuredValueTests(unittest.TestCase):
    """Drop tables are server-side, so worth is learned rather than looked up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = CombatMemory(path=Path(self.tmp.name) / "combat.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _fight(self, monster_id, xp=22, items=None, wins=True, turns=4, damage=5):
        self.memory.record_battle(monster_id, {
            "winner": "player" if wins else "monster", "xp": xp,
            "items": items or []}, turns, damage)

    def test_unfought_monster_is_worth_nothing_measurable(self):
        from slcw.combat import expected_value
        self.assertEqual(expected_value("bigfrog_lvl1_1", self.memory), 0.0)

    def test_value_rises_with_observed_xp(self):
        from slcw.combat import expected_value
        self._fight("bigfrog_lvl1_1", xp=22)
        self._fight("forestspider_lvl1_2", xp=100)
        self.assertGreater(expected_value("forestspider_lvl1_2", self.memory),
                           expected_value("bigfrog_lvl1_1", self.memory))

    def test_drops_are_priced_from_the_live_market(self):
        from slcw.combat import expected_value
        from slcw.market import build_snapshot
        market = build_snapshot([{"status": "open", "type": "buy",
                                  "templateId": "frogslime", "price": 500,
                                  "quantity": 99, "filled": 0}])
        self._fight("bigfrog_lvl1_1", xp=1, items=[{"id": "frogslime", "quantity": 2}])
        with_market = expected_value("bigfrog_lvl1_1", self.memory, market)
        without = expected_value("bigfrog_lvl1_1", self.memory, None)
        self.assertGreater(with_market, without)

    def test_losses_discount_the_value(self):
        from slcw.combat import expected_value
        for _ in range(4):
            self._fight("bigfrog_lvl1_1", xp=22, wins=True)
            self._fight("forestspider_lvl1_2", xp=22, wins=False)
        self.assertGreater(expected_value("bigfrog_lvl1_1", self.memory),
                           expected_value("forestspider_lvl1_2", self.memory))

    def test_damage_taken_is_charged_against_the_reward(self):
        """The measured case, in its measured shape: two monsters that pay
        almost the same xp, where one of them costs four times the hit points.
        Ranking on reward alone called those two equal for weeks."""
        from slcw.combat import expected_value
        for _ in range(5):
            self._fight("bigfrog_lvl1_1", xp=66, damage=28)
            self._fight("forestspider_lvl1_2", xp=64, damage=127)
        self.assertGreater(expected_value("bigfrog_lvl1_1", self.memory),
                           expected_value("forestspider_lvl1_2", self.memory))

    def test_a_cheap_fight_can_beat_a_richer_one(self):
        """Not merely a tie-breaker: enough damage outweighs a real edge in xp."""
        from slcw.combat import expected_value
        for _ in range(5):
            self._fight("bigfrog_lvl1_1", xp=50, damage=10)
            self._fight("forestspider_lvl1_2", xp=60, damage=200)
        self.assertGreater(expected_value("bigfrog_lvl1_1", self.memory),
                           expected_value("forestspider_lvl1_2", self.memory))

    def test_free_damage_is_still_ranked_on_reward(self):
        from slcw.combat import expected_value
        for _ in range(5):
            self._fight("bigfrog_lvl1_1", xp=10, damage=0)
            self._fight("forestspider_lvl1_2", xp=90, damage=0)
        self.assertGreater(expected_value("forestspider_lvl1_2", self.memory),
                           expected_value("bigfrog_lvl1_1", self.memory))

    def test_selection_avoids_the_monster_that_costs_the_most_health(self):
        rng = random.Random(4)
        for _ in range(6):
            self._fight("bigfrog_lvl1_1", xp=60, damage=12)
            self._fight("forestspider_lvl1_2", xp=62, damage=120)
        picks = [select_monster(["bigfrog_lvl1_1", "forestspider_lvl1_2"],
                                player_level=1, health_ratio=1.0,
                                memory=self.memory, rng=rng) for _ in range(30)]
        self.assertGreater(picks.count("bigfrog_lvl1_1"),
                           picks.count("forestspider_lvl1_2"))

    def test_selection_prefers_the_monster_that_actually_paid(self):
        rng = random.Random(4)
        for _ in range(6):
            self._fight("bigfrog_lvl1_1", xp=5)
            self._fight("forestspider_lvl1_2", xp=200)
        picks = [select_monster(["bigfrog_lvl1_1", "forestspider_lvl1_2"],
                                player_level=1, health_ratio=1.0,
                                memory=self.memory, rng=rng) for _ in range(30)]
        self.assertGreater(picks.count("forestspider_lvl1_2"),
                           picks.count("bigfrog_lvl1_1"))

    def test_something_untried_is_still_tried(self):
        """Without discovery the bot would farm its first monster forever."""
        rng = random.Random(1)
        for _ in range(10):
            self._fight("bigfrog_lvl1_1", xp=500)
        seen = {select_monster(["bigfrog_lvl1_1", "forestspider_lvl1_2"],
                               player_level=1, health_ratio=1.0,
                               memory=self.memory, rng=rng) for _ in range(200)}
        self.assertIn("forestspider_lvl1_2", seen)

    def test_battle_outcomes_survive_a_reload(self):
        self._fight("bigfrog_lvl1_1", xp=22, items=[{"id": "frogslime", "quantity": 3}])
        self.memory.save()
        reloaded = CombatMemory(path=self.memory.path)
        model = reloaded.models["bigfrog_lvl1_1"]
        self.assertEqual(model.battles, 1)
        self.assertEqual(model.drops["frogslime"], 3)
        self.assertEqual(model.avg_xp, 22)


class BestSourceTests(unittest.TestCase):
    """Which monster to fight when a specific item is what we are after.

    Nothing else in the bot answers this question. Monster choice is driven by
    gold-equivalent value, and a clan quest asks for raw drops that have no
    market bids at all — so the monster that supplies them scores zero and is
    never picked. The fleet's own clan quest sat at 471 of 2,000 frogslime on
    2026-08-21, gaining four an hour by accident, against an expiry six days
    out: it could not have finished.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = CombatMemory(path=Path(self.tmp.name) / "combat.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _fight(self, monster_id, items, times=1):
        for _ in range(times):
            self.memory.record_battle(
                monster_id, {"winner": "player", "xp": 10, "items": items}, 4, 5)

    def test_the_monster_that_drops_it_is_named(self):
        from slcw.combat import best_source
        self._fight("bigfrog_lvl1_1", [{"id": "frogslime", "quantity": 2}], times=5)
        self._fight("icewolf_lvl2_3", [{"id": "frostpelt", "quantity": 2}], times=5)
        self.assertEqual(best_source("frogslime", self.memory), "bigfrog_lvl1_1")

    def test_an_item_nothing_has_dropped_has_no_source(self):
        from slcw.combat import best_source
        self._fight("bigfrog_lvl1_1", [{"id": "frogslime", "quantity": 2}], times=5)
        self.assertIsNone(best_source("imperialseal", self.memory))

    def test_the_easier_monster_wins_when_the_rates_are_close(self):
        """A level-13 frog dropping 1.55 an hour beats a level-1 frog dropping
        1.48 on paper, and loses badly in practice: the fight is longer, the
        damage is real, and the item is the same."""
        from slcw.combat import best_source
        self._fight("bigfrog_lvl13_2", [{"id": "frogslime", "quantity": 31}], times=20)
        self._fight("bigfrog_lvl1_1", [{"id": "frogslime", "quantity": 30}], times=20)
        self.assertEqual(best_source("frogslime", self.memory), "bigfrog_lvl1_1")

    def test_a_clearly_better_source_beats_a_lower_level_one(self):
        from slcw.combat import best_source
        self._fight("bigfrog_lvl1_1", [{"id": "frogslime", "quantity": 1}], times=20)
        self._fight("bigfrog_lvl13_2", [{"id": "frogslime", "quantity": 9}], times=20)
        self.assertEqual(best_source("frogslime", self.memory), "bigfrog_lvl13_2")

    def test_monsters_above_the_players_level_are_not_offered(self):
        from slcw.combat import best_source
        self._fight("bigfrog_lvl13_2", [{"id": "frogslime", "quantity": 9}], times=20)
        self._fight("bigfrog_lvl1_1", [{"id": "frogslime", "quantity": 1}], times=20)
        self.assertEqual(best_source("frogslime", self.memory, max_level=5),
                         "bigfrog_lvl1_1")

    def test_no_memory_at_all_names_nothing(self):
        from slcw.combat import best_source
        self.assertIsNone(best_source("frogslime", None))
        self.assertIsNone(best_source("", self.memory))


class ReachTests(unittest.TestCase):
    """The level band the server will actually let us fight in.

    There is a floor as well as a ceiling, and only the ceiling was ever
    modelled. Measured live on 2026-08-22 with a level-15 character standing in
    farm_3: startBattle against a level-9 monster answers "Monster level is
    outside your reach", and level 10 starts a fight. That refusal classifies
    as benign, so a wallet that keeps choosing an out-of-reach monster reports
    no error at all — which is exactly what seven wallets did for half an hour
    after the clan quest errand first shipped, before this floor existed.
    """

    def test_a_monster_five_levels_down_is_still_in_reach(self):
        from slcw.combat import in_reach
        self.assertTrue(in_reach("aerial_lvl10_1", 15))

    def test_a_monster_six_levels_down_is_not(self):
        from slcw.combat import in_reach
        self.assertFalse(in_reach("icewolf_lvl9_1", 15))

    def test_a_monster_at_our_own_level_is_in_reach(self):
        from slcw.combat import in_reach
        self.assertTrue(in_reach("bigfrog_lvl13_2", 13))

    def test_an_unknown_monster_is_left_alone(self):
        """Not in the registry means no level to judge, and refusing it here
        would hide a monster the server might well accept."""
        from slcw.combat import in_reach
        self.assertTrue(in_reach("nonesuch_lvl1_1", 15))


class SourceReachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = CombatMemory(path=Path(self.tmp.name) / "combat.json")
        for monster in ("bigfrog_lvl1_1", "bigfrog_lvl13_2"):
            for _ in range(10):
                self.memory.record_battle(
                    monster, {"winner": "player", "xp": 5,
                              "items": [{"id": "frogslime", "quantity": 2}]}, 3, 4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_out_of_reach_source_is_skipped_for_one_we_can_fight(self):
        from slcw.combat import best_source
        self.assertEqual(best_source("frogslime", self.memory, min_level=10),
                         "bigfrog_lvl13_2")

    def test_without_a_floor_the_easiest_source_still_wins(self):
        from slcw.combat import best_source
        self.assertEqual(best_source("frogslime", self.memory), "bigfrog_lvl1_1")

    def test_a_floor_above_every_source_names_nothing(self):
        from slcw.combat import best_source
        self.assertIsNone(best_source("frogslime", self.memory, min_level=40))


class SelectionReachTests(unittest.TestCase):
    """Ordinary monster choice has the same floor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = CombatMemory(path=Path(self.tmp.name) / "combat.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_monster_below_the_floor_is_never_picked(self):
        from slcw.combat import select_monster
        for _ in range(10):
            self.memory.record_battle(
                "bigfrog_lvl1_1", {"winner": "player", "xp": 9_999, "items": []}, 3, 4)
            self.memory.record_battle(
                "aerial_lvl10_1", {"winner": "player", "xp": 1, "items": []}, 3, 4)
        picked = {select_monster(["bigfrog_lvl1_1", "aerial_lvl10_1"], 15, 1.0,
                                 memory=self.memory) for _ in range(20)}
        self.assertNotIn("bigfrog_lvl1_1", picked)
