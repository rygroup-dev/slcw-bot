import math
import unittest

from slcw import economy as econ
from slcw import refining, world
from slcw.config import Config
from slcw.market import build_snapshot
from slcw.model import parse_player
from tests.test_orchestrator import FakeApi, make


def bids(**prices):
    return build_snapshot([
        {"status": "open", "type": "buy", "templateId": item,
         "price": price, "quantity": 9999, "filled": 0}
        for item, price in prices.items()])


class MapTests(unittest.TestCase):
    def test_catalog_covers_cities_and_farm_zones(self):
        cities = [l for l in world.LOCATIONS.values() if l.is_city]
        farms = [l for l in world.LOCATIONS.values() if l.is_farm]
        self.assertEqual(len(cities), 12)
        self.assertEqual(len(farms), 5)

    def test_every_workshop_city_is_on_the_map(self):
        for workshop in refining.WORKSHOPS.values():
            self.assertIn(workshop.city_id, world.LOCATIONS)

    def test_every_gathering_site_is_on_the_map(self):
        from slcw.farming import FARM_LOCATIONS
        for site in FARM_LOCATIONS:
            self.assertIn(site, world.LOCATIONS)

    def test_distance_is_symmetric(self):
        self.assertAlmostEqual(world.distance("city_1", "farm_2"),
                               world.distance("farm_2", "city_1"))

    def test_distance_to_self_is_zero(self):
        self.assertEqual(world.distance("city_1", "city_1"), 0.0)

    def test_unknown_location_is_unreachable(self):
        self.assertEqual(world.distance("city_1", "atlantis"), float("inf"))
        self.assertEqual(world.travel_seconds("city_1", "atlantis"), float("inf"))

    def test_travel_time_matches_the_client_formula(self):
        # city_1 (15.5, 35) to farm_2 (10, 20)
        gap = math.hypot(10 - 15.5, 20 - 35)
        self.assertAlmostEqual(world.travel_seconds("city_1", "farm_2"),
                               round(20 * gap), delta=1)

    def test_mount_speed_reduces_travel_time(self):
        plain = world.travel_seconds("city_1", "farm_3")
        mounted = world.travel_seconds("city_1", "farm_3", speed_reduction=0.5)
        self.assertLess(mounted, plain)
        self.assertAlmostEqual(mounted, plain / 2, delta=2)

    def test_speed_reduction_is_clamped_at_the_client_ceiling(self):
        capped = world.travel_seconds("city_1", "farm_3", speed_reduction=0.99)
        expected = world.travel_seconds("city_1", "farm_3", speed_reduction=0.75)
        self.assertEqual(capped, expected)

    def test_smelting_loop_is_short(self):
        """farm_2 gathers copper ore, city_1 smelts it — the tightest chain."""
        self.assertLess(world.travel_seconds("farm_2", "city_1"), 400)

    def test_economic_locations_cover_the_whole_chain(self):
        places = world.economic_locations()
        for workshop in refining.WORKSHOPS.values():
            self.assertIn(workshop.city_id, places)
        self.assertIn("city_2", places)
        self.assertIn("farm_2", places)

    def test_nearest_ignores_the_current_location(self):
        self.assertNotEqual(world.nearest("city_1", ["city_1", "farm_2"]), "city_1")

    def test_names_resolve(self):
        self.assertEqual(world.name_of("city_1"), "Agnos")
        self.assertEqual(world.name_of("nowhere"), "nowhere")


class TravelPlanningTests(unittest.TestCase):
    HOLDINGS = {"copper_ore": 900, "smelting_flux_1": 100}

    def _state(self, location, **overrides):
        doc = {"level": 6, "grade": 1, "energy": 80, "maxEnergy": 100,
               "balance": 50000, "currentHealth": 130, "currentMana": 130,
               "currentLocationId": location, "attributePoints": 0,
               "attributes": {"vitality": 3, "wisdom": 3},
               "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
               "freeEnergyRefillsToday": 3,
               "lastFreeEnergyRefillDate": "2099-01-01"}
        doc.update(overrides)
        return parse_player(doc)

    def test_travels_to_the_workshop_when_holding_refinable_material(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state("farm_3"), bids(copper_ingot=888), self.HOLDINGS)
        top = candidates[0]
        self.assertEqual(top.action, "startTravel")
        self.assertEqual(top.params["destinationId"], "city_1")

    def test_travel_can_be_disabled(self):
        orchestrator = make(
            config=Config(enabled=True, dry_run=False, auto_travel=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state("farm_3"), bids(copper_ingot=888), self.HOLDINGS)
        self.assertNotIn("startTravel", [c.action for c in candidates])

    def test_stays_put_when_already_at_the_best_location(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state("city_1"), bids(copper_ingot=888), self.HOLDINGS)
        self.assertEqual(candidates[0].action, "startRefining")

    def test_margin_prevents_travelling_for_a_marginal_gain(self):
        greedy = make(config=Config(enabled=True, dry_run=False,
                                    travel_margin=1.0), api=FakeApi())
        cautious = make(config=Config(enabled=True, dry_run=False,
                                      travel_margin=1000.0), api=FakeApi())
        state = self._state("city_2")
        market = bids(copper_ingot=888)
        self.assertIn("startTravel",
                      [c.action for c in greedy.build_candidates(state, market, self.HOLDINGS)])
        self.assertNotIn("startTravel",
                         [c.action for c in cautious.build_candidates(state, market, self.HOLDINGS)])

    def test_travel_candidate_reports_its_destination_and_purpose(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state("farm_3"), bids(copper_ingot=888), self.HOLDINGS)
        travel = next(c for c in candidates if c.action == "startTravel")
        self.assertIn("Agnos", travel.reason)
        self.assertIn("startRefining", travel.reason)

    def test_travel_never_targets_the_current_location(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        for location in ("city_1", "farm_2", "city_2", "farm_3"):
            for candidate in orchestrator.build_candidates(
                    self._state(location), bids(copper_ingot=888), self.HOLDINGS):
                if candidate.action == "startTravel":
                    self.assertNotEqual(candidate.params["destinationId"], location)

    def test_leaves_a_location_that_offers_nothing(self):
        """city_17 has no workshop, no gathering, and no production."""
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state("city_17"), build_snapshot([]), {})
        self.assertEqual(candidates[0].action, "startTravel")
        self.assertIn(candidates[0].params["destinationId"],
                      world.economic_locations())

    def test_disabled_engine_never_travels(self):
        orchestrator = make(config=Config(enabled=False, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state("city_17"), bids(copper_ingot=888), self.HOLDINGS)
        self.assertNotIn("startTravel", [c.action for c in candidates])

    def test_busy_wallet_is_never_sent_travelling(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        state = self._state("farm_3", activity={"type": "farming",
                                                "endTime": {"seconds": 9999999999}})
        self.assertEqual(orchestrator.build_candidates(
            state, bids(copper_ingot=888), self.HOLDINGS), [])

    def test_executor_sends_the_destination(self):
        api = FakeApi()
        sent = {}
        api.start_travel = lambda s, dest: sent.setdefault("dest", dest) or {"ok": True}
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state("farm_3"),
            bids(copper_ingot=888), self.HOLDINGS)
        self.assertEqual(decision.action, "startTravel")
        self.assertEqual(sent["dest"], "city_1")


if __name__ == "__main__":
    unittest.main()


class StabilityTests(unittest.TestCase):
    """Repeat-aware travel must not turn into pacing between two locations."""

    def _state(self, location, energy=85, gold=0, health=130):
        return parse_player({
            "level": 15, "grade": 1, "xp": 0, "energy": energy, "maxEnergy": 100,
            "balance": gold, "currentHealth": health, "currentMana": 130,
            "currentLocationId": location, "attributePoints": 0,
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 16)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"})

    def test_a_wallet_settles_instead_of_pacing(self):
        """Simulate arriving and acting repeatedly; it must stop relocating."""
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        location = "city_2"
        moves = []
        energy = 85

        for _ in range(12):
            candidates = orchestrator.build_candidates(
                self._state(location, energy=energy), None, {})
            if not candidates:
                break
            top = candidates[0]
            if top.action == "startTravel":
                location = top.params["destinationId"]
                moves.append(location)
            else:
                # Acting consumes energy, which is what eventually ends the stay.
                energy = max(0, energy - max(1, top.energy_cost))

        self.assertLessEqual(len(moves), 2,
                             f"wallet relocated {len(moves)} times: {moves}")

    def test_arriving_somewhere_it_does_not_immediately_leave(self):
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        candidates = orchestrator.build_candidates(self._state("farm_3"), None, {})
        self.assertEqual(candidates[0].action, "battle",
                         "a wallet at a battle site with energy should fight, not move")

    def test_margin_is_what_prevents_oscillation(self):
        """With no margin the engine would chase every marginal difference."""
        greedy = make(config=Config(enabled=True, dry_run=True, travel_margin=1.0),
                      api=FakeApi())
        guarded = make(config=Config(enabled=True, dry_run=True, travel_margin=1.35),
                       api=FakeApi())
        state = self._state("farm_3")
        greedy_top = greedy.build_candidates(state, None, {})[0]
        guarded_top = guarded.build_candidates(state, None, {})[0]
        self.assertEqual(guarded_top.action, "battle")
        self.assertIn(greedy_top.action, ("battle", "startTravel"))

    def test_projection_is_bounded_by_energy(self):
        from slcw.orchestrator import MAX_PROJECTED_REPEATS
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        drained = orchestrator._repeats_at(
            econ.ActionScore(action="battle", energy_cost=1, duration_seconds=45),
            self._state("city_2", energy=3))
        self.assertEqual(drained, 3)

        plenty = orchestrator._repeats_at(
            econ.ActionScore(action="battle", energy_cost=1, duration_seconds=45),
            self._state("city_2", energy=100))
        self.assertEqual(plenty, MAX_PROJECTED_REPEATS)

    def test_zero_energy_actions_are_not_projected_to_repeat(self):
        """Gold-mode farming spends its whole budget in one call."""
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        self.assertEqual(orchestrator._repeats_at(
            econ.ActionScore(action="startFarming", energy_cost=0,
                             duration_seconds=28800),
            self._state("farm_2", energy=100)), 1)

    def test_an_exhausted_wallet_stops_projecting_a_long_stay(self):
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        self.assertEqual(orchestrator._repeats_at(
            econ.ActionScore(action="battle", energy_cost=1, duration_seconds=45),
            self._state("city_2", energy=0)), 1)
