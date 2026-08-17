import unittest

from slcw import economy as econ
from slcw.model import parse_player


class EnergyPriceTests(unittest.TestCase):
    def setUp(self):
        self.economy = econ.Economy()

    def test_energy_is_nearly_free_when_full(self):
        self.assertAlmostEqual(self.economy.energy_price(100, 100), 0.0)

    def test_energy_is_expensive_when_empty(self):
        self.assertAlmostEqual(self.economy.energy_price(0, 100), econ.DEFAULT_ENERGY_GOLD)

    def test_price_rises_monotonically_as_energy_drains(self):
        prices = [self.economy.energy_price(level, 100) for level in (100, 75, 50, 25, 0)]
        self.assertEqual(prices, sorted(prices))


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.economy = econ.Economy()

    def test_production_scores_by_gold_per_hour_when_energy_abundant(self):
        candidate = self.economy.score_action(econ.production_candidate(), 100, 100)
        # 1000 gold over 18 minutes, energy free at full bar.
        self.assertAlmostEqual(candidate.score, 1000 / 0.3, delta=1.0)

    def test_scarce_energy_penalises_the_energy_hungry_action(self):
        abundant = self.economy.score_action(econ.production_candidate(), 100, 100)
        scarce = self.economy.score_action(econ.production_candidate(), 10, 100)
        self.assertLess(scarce.score, abundant.score)

    def test_battle_beats_production_per_energy_when_drops_are_valuable(self):
        battle = econ.battle_candidate(
            "forestspider_lvl1_2", self.economy,
            drop_values={"spiderfang": 400.0}, expected_drops={"spiderfang": 1.5})
        production = econ.production_candidate()
        self.assertGreater(battle.gold_per_energy, production.gold_per_energy)

    def test_battle_without_market_data_is_marked_degraded(self):
        battle = econ.battle_candidate(
            "forestspider_lvl1_2", self.economy, drop_values={}, market_stale=True)
        self.assertTrue(battle.degraded)
        self.assertIn("stale", battle.reason)
        # XP still carries value, so the action is not worthless.
        self.assertGreater(battle.gold_equivalent, 0)

    def test_free_actions_outrank_everything(self):
        free = econ.free_candidate("finishActivity", {}, "reward waiting")
        production = self.economy.score_action(econ.production_candidate(), 100, 100)
        self.assertEqual(econ.rank([production, free])[0].action, "finishActivity")

    def test_rank_orders_by_score_descending(self):
        high = econ.ActionScore(action="high", score=500.0, duration_seconds=60)
        low = econ.ActionScore(action="low", score=10.0, duration_seconds=60)
        self.assertEqual([c.action for c in econ.rank([low, high])], ["high", "low"])

    def test_rank_breaks_ties_with_shorter_duration(self):
        slow = econ.ActionScore(action="slow", score=100.0, duration_seconds=600)
        fast = econ.ActionScore(action="fast", score=100.0, duration_seconds=60)
        self.assertEqual(econ.rank([slow, fast])[0].action, "fast")


class HuntingTests(unittest.TestCase):
    def setUp(self):
        self.economy = econ.Economy()

    def _state(self, gold=1000, energy=50):
        return parse_player({"balance": gold, "energy": energy, "maxEnergy": 100,
                             "attributes": {"vitality": 3, "wisdom": 3},
                             "currentHealth": 130, "currentMana": 130})

    def test_scores_using_the_measured_yield(self):
        hunt = econ.hunting_candidate(self._state(), self.economy)
        self.assertEqual(hunt.action, "startHunting")
        self.assertEqual(hunt.energy_cost, 3)
        self.assertEqual(hunt.gold_cost, 3)
        self.assertAlmostEqual(hunt.gold_equivalent, 11 * econ.DEFAULT_XP_GOLD)

    def test_none_without_enough_energy(self):
        self.assertIsNone(econ.hunting_candidate(self._state(energy=1), self.economy))

    def test_none_without_enough_gold(self):
        self.assertIsNone(econ.hunting_candidate(self._state(gold=0), self.economy))

    def test_loses_to_battle_on_energy_efficiency(self):
        """Measured live: 11 xp for 3 energy vs battle's 22 xp for 1 — battle wins."""
        hunt = econ.hunting_candidate(self._state(), self.economy)
        battle = econ.battle_candidate("forestspider_lvl1_2", self.economy)
        self.assertLess(hunt.gold_per_energy, battle.gold_per_energy)


class RelaxTests(unittest.TestCase):
    def test_relax_value_tracks_missing_health(self):
        hurt = parse_player({"attributes": {"vitality": 3, "wisdom": 3},
                             "currentHealth": 30, "currentMana": 130})
        healthy = parse_player({"attributes": {"vitality": 3, "wisdom": 3},
                                "currentHealth": 129, "currentMana": 130})
        self.assertGreater(econ.relax_candidate(hurt).gold_equivalent,
                           econ.relax_candidate(healthy).gold_equivalent)


if __name__ == "__main__":
    unittest.main()
