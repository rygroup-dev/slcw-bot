import unittest

from slcw import economy as econ
from slcw import farming
from slcw.config import Config
from slcw.market import build_snapshot
from slcw.model import parse_player
from slcw.orchestrator import Orchestrator, _GoldBudget
from tests.test_orchestrator import FakeApi, make


class CostModelTests(unittest.TestCase):
    """Formulas taken verbatim from the frontend bundle."""

    def test_tier_multiplier_is_three_to_the_tier(self):
        self.assertEqual(farming.tier_multiplier(1), 1)
        self.assertEqual(farming.tier_multiplier(3), 9)
        self.assertEqual(farming.tier_multiplier(7), 729)

    def test_energy_mode_costs_one_energy_per_cycle(self):
        cost = farming.energy_mode_cost(tier=1, cycles=30)
        self.assertEqual(cost, {"gold": 30, "energy": 30, "diamonds": 0})

    def test_energy_mode_gold_scales_with_tier(self):
        self.assertEqual(farming.energy_mode_cost(tier=3, cycles=10)["gold"], 90)

    def test_gold_mode_spends_no_energy(self):
        cost = farming.gold_mode_cost(tier=1, hours=8)
        self.assertEqual(cost["energy"], 0)
        # round(8/8*1500) + 60*8*1 = 1500 + 480
        self.assertEqual(cost["gold"], 1980)

    def test_gold_mode_partial_hours(self):
        # round(4/8*1500) + 60*4*1 = 750 + 240
        self.assertEqual(farming.gold_mode_cost(tier=1, hours=4)["gold"], 990)

    def test_energy_cycles_capped_by_the_scarcest_resource(self):
        self.assertEqual(farming.max_energy_cycles(1, energy=500, gold=500), 100)
        self.assertEqual(farming.max_energy_cycles(1, energy=12, gold=500), 12)
        self.assertEqual(farming.max_energy_cycles(3, energy=500, gold=45), 5)

    def test_no_cycles_without_gold(self):
        self.assertEqual(farming.max_energy_cycles(1, energy=50, gold=0), 0)


class CatalogTests(unittest.TestCase):
    def test_all_four_gathering_sites_present(self):
        self.assertEqual(set(farming.FARM_LOCATIONS),
                         {"farm_1", "farm_2", "farm_4", "farm_5"})

    def test_farm_3_is_not_a_gathering_site(self):
        # farm_3 is where battles happen; treating it as a farm would send a
        # startFarming call the server would reject.
        self.assertNotIn("farm_3", farming.FARM_LOCATIONS)
        self.assertEqual(farming.resources_at("farm_3"), [])

    def test_grade_gates_higher_tiers(self):
        eligible = farming.eligible_resources("farm_2", level=99, grade=1)
        self.assertEqual([r.item_id for r in eligible], ["copper_ore"])

    def test_level_gates_resources_too(self):
        eligible = farming.eligible_resources("farm_2", level=0, grade=7)
        self.assertEqual([r.item_id for r in eligible], ["copper_ore"])

    def test_higher_grade_unlocks_the_ladder(self):
        eligible = farming.eligible_resources("farm_2", level=45, grade=4)
        self.assertEqual([r.item_id for r in eligible],
                         ["copper_ore", "iron_ore", "steel_ore", "mithril_ore"])

    def test_best_resource_prefers_the_highest_bid(self):
        market = build_snapshot([
            {"status": "open", "type": "buy", "templateId": "copper_ore",
             "price": 900, "quantity": 10, "filled": 0},
            {"status": "open", "type": "buy", "templateId": "iron_ore",
             "price": 100, "quantity": 10, "filled": 0},
        ])
        best = farming.best_resource("farm_2", level=45, grade=4, market=market)
        self.assertEqual(best.item_id, "copper_ore")

    def test_best_resource_falls_back_to_tier_without_prices(self):
        best = farming.best_resource("farm_2", level=45, grade=4, market=None)
        self.assertEqual(best.item_id, "mithril_ore")

    def test_payload_matches_the_frontend_shape(self):
        resource = farming.resources_at("farm_2")[0]
        payload = farming.build_payload(resource, "energy", cycles=25)
        self.assertEqual(payload, {"itemId": "copper_ore", "mode": "energy",
                                   "cycles": 25, "hours": 0,
                                   "profession": "miner", "tier": 1})

    def test_gold_mode_payload_converts_hours_to_cycles(self):
        resource = farming.resources_at("farm_1")[0]
        payload = farming.build_payload(resource, "gold", hours=8)
        self.assertEqual(payload["cycles"], 480)
        self.assertEqual(payload["hours"], 8)


class FarmingScoringTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(enabled=True, dry_run=False, farming_gold_hours=8)
        self.resource = farming.resources_at("farm_2")[0]

    def _state(self, gold=5000, energy=80):
        return _GoldBudget(
            parse_player({"attributes": {"vitality": 3, "wisdom": 3},
                          "currentHealth": 130, "currentMana": 130,
                          "energy": energy, "balance": gold}),
            gold)

    def test_valuable_resource_produces_positive_candidates(self):
        market = build_snapshot([{"status": "open", "type": "buy",
                                  "templateId": "copper_ore", "price": 400,
                                  "quantity": 999, "filled": 0}])
        candidates = econ.farming_candidates(
            self.resource, self._state(), market, self.config)
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertGreater(candidate.net_gold, 0)
            self.assertFalse(candidate.degraded)

    def test_unpriced_resource_is_valued_at_zero_and_flagged(self):
        candidates = econ.farming_candidates(
            self.resource, self._state(), build_snapshot([]), self.config)
        for candidate in candidates:
            self.assertEqual(candidate.gold_equivalent, 0)
            self.assertTrue(candidate.degraded)
            self.assertIn("no market bid", candidate.reason)

    def test_gold_mode_candidate_uses_no_energy(self):
        market = build_snapshot([{"status": "open", "type": "buy",
                                  "templateId": "copper_ore", "price": 400,
                                  "quantity": 999, "filled": 0}])
        candidates = econ.farming_candidates(
            self.resource, self._state(energy=0), market, self.config)
        self.assertEqual(len(candidates), 1, "only gold mode survives with no energy")
        self.assertEqual(candidates[0].energy_cost, 0)
        self.assertEqual(candidates[0].params["mode"], "gold")

    def test_gold_mode_skipped_when_unaffordable(self):
        candidates = econ.farming_candidates(
            self.resource, self._state(gold=10, energy=0),
            build_snapshot([]), self.config)
        self.assertEqual(candidates, [])


class FarmingIntegrationTests(unittest.TestCase):
    RICH_MARKET = build_snapshot([{"status": "open", "type": "buy",
                                   "templateId": "copper_ore", "price": 600,
                                   "quantity": 9999, "filled": 0}])

    def _state(self, **overrides):
        doc = {"level": 6, "grade": 1, "energy": 80, "maxEnergy": 100,
               "balance": 5000, "currentHealth": 130, "currentMana": 130,
               "currentLocationId": "farm_2", "attributePoints": 0,
               "attributes": {"vitality": 3, "wisdom": 3},
               "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None}
        doc.update(overrides)
        return parse_player(doc)

    def test_orchestrator_farms_at_a_gathering_site(self):
        api = FakeApi()
        api.start_farming = lambda session, payload: api._record("startFarming", **payload)
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), self.RICH_MARKET)
        self.assertEqual(decision.action, "startFarming")
        self.assertEqual(decision.params["itemId"], "copper_ore")

    def test_gold_reserve_is_never_spent(self):
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, gold_reserve=4800), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state(balance=5000), self.RICH_MARKET)
        for candidate in candidates:
            self.assertLessEqual(candidate.gold_cost, 200)

    def test_unprofitable_farming_is_rejected(self):
        # copper_ore has no bid here, so every farming candidate nets negative.
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(self._state(), build_snapshot([]))
        self.assertEqual([c for c in candidates if c.action == "startFarming"], [])

    def test_farming_never_offered_at_a_non_farm_location(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state(currentLocationId="city_2"), self.RICH_MARKET)
        self.assertNotIn("startFarming", [c.action for c in candidates])


class PaginationTests(unittest.TestCase):
    """A single 500-row query saw only 500 of 5,865 live market orders."""

    class PagingApi:
        def __init__(self, total):
            self.total = total
            self.offsets = []

        def query_collection(self, session, collection, limit=500, offset=0):
            self.offsets.append(offset)
            remaining = max(0, self.total - offset)
            return [{"id": offset + i} for i in range(min(limit, remaining))]

    def test_pages_until_the_collection_is_exhausted(self):
        from slcw.api import GameApi

        api = self.PagingApi(total=1250)
        rows = GameApi.query_all(api, None, "blackmarket_orders", page_size=500)
        self.assertEqual(len(rows), 1250)
        self.assertEqual(api.offsets, [0, 500, 1000])

    def test_stops_on_a_short_page(self):
        from slcw.api import GameApi

        api = self.PagingApi(total=120)
        rows = GameApi.query_all(api, None, "blackmarket_orders", page_size=500)
        self.assertEqual(len(rows), 120)
        self.assertEqual(api.offsets, [0])

    def test_respects_the_page_ceiling(self):
        from slcw.api import GameApi

        api = self.PagingApi(total=10**6)
        GameApi.query_all(api, None, "blackmarket_orders", page_size=500, max_pages=3)
        self.assertEqual(len(api.offsets), 3)


if __name__ == "__main__":
    unittest.main()
