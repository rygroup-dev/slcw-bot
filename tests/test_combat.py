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


if __name__ == "__main__":
    unittest.main()
