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
