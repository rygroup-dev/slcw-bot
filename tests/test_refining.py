import unittest

from slcw import economy as econ
from slcw import refining
from slcw.config import Config
from slcw.market import build_snapshot
from slcw.model import parse_player
from slcw.orchestrator import Orchestrator
from tests.test_orchestrator import FakeApi, make


def bids(**prices):
    return build_snapshot([
        {"status": "open", "type": "buy", "templateId": item,
         "price": price, "quantity": 9999, "filled": 0}
        for item, price in prices.items()])


class CatalogTests(unittest.TestCase):
    def test_all_four_workshops_present(self):
        self.assertEqual(set(refining.WORKSHOPS),
                         {"smelting", "sawmill", "tanning", "weaving"})

    def test_each_workshop_lives_in_its_own_city(self):
        cities = [w.city_id for w in refining.WORKSHOPS.values()]
        self.assertEqual(len(set(cities)), 4)

    def test_workshop_lookup_by_city(self):
        self.assertEqual(refining.workshop_at("city_1").id, "smelting")
        self.assertEqual(refining.workshop_at("city_18").id, "weaving")
        self.assertIsNone(refining.workshop_at("farm_2"))

    def test_every_output_maps_to_a_raw_material(self):
        for workshop in refining.WORKSHOPS.values():
            for item in workshop.items:
                self.assertTrue(workshop.raw_for(item), f"{item} has no raw material")

    def test_each_workshop_covers_seven_tiers(self):
        for workshop in refining.WORKSHOPS.values():
            self.assertEqual(len(workshop.items), 7)

    def test_tier_lookup_round_trips(self):
        smelting = refining.WORKSHOPS["smelting"]
        self.assertEqual(smelting.tier_of("copper_ingot"), 1)
        self.assertEqual(smelting.tier_of("astralius_ingot"), 7)
        self.assertEqual(smelting.output_for_tier(3), "steel_ingot")
        self.assertEqual(smelting.tier_of("not_a_thing"), 0)

    def test_catalyst_names_are_tier_suffixed(self):
        self.assertEqual(refining.WORKSHOPS["smelting"].catalyst_for(1), "smelting_flux_1")
        self.assertEqual(refining.WORKSHOPS["weaving"].catalyst_for(7), "fixative_7")

    def test_professions_map_to_their_gathering_sites(self):
        for workshop in refining.WORKSHOPS.values():
            farm = refining.PROFESSION_FARM[workshop.profession]
            from slcw.farming import FARM_LOCATIONS
            self.assertIn(farm, FARM_LOCATIONS)
            self.assertEqual(FARM_LOCATIONS[farm]["profession"], workshop.profession)

    def test_gathered_raw_materials_match_refining_inputs(self):
        """The two catalogs must agree, or the chain has a hole."""
        from slcw.farming import FARM_LOCATIONS
        for workshop in refining.WORKSHOPS.values():
            farm = refining.PROFESSION_FARM[workshop.profession]
            gathered = {r["itemId"] for r in FARM_LOCATIONS[farm]["resources"]}
            for item in workshop.items:
                self.assertIn(workshop.raw_for(item), gathered,
                              f"{workshop.id} needs {workshop.raw_for(item)}, "
                              f"which {farm} does not produce")


class RecipeTests(unittest.TestCase):
    SMELTING = refining.WORKSHOPS["smelting"]

    def test_tier_one_consumes_nine_raw_per_cycle(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=2)
        self.assertEqual(recipe.raw_item, "copper_ore")
        self.assertEqual(recipe.raw_needed, 18)
        self.assertEqual(recipe.catalyst_needed, 2)
        self.assertIsNone(recipe.previous_item)

    def test_higher_tiers_consume_three_raw_and_the_previous_tier(self):
        recipe = refining.Recipe(self.SMELTING, "iron_ingot", 2, cycles=4)
        self.assertEqual(recipe.raw_needed, 12)
        self.assertEqual(recipe.previous_item, "copper_ingot")
        self.assertEqual(recipe.previous_needed, 4)

    def test_gold_and_duration_scale_with_tier(self):
        self.assertEqual(refining.Recipe(self.SMELTING, "copper_ingot", 1, 10).gold_cost, 50)
        self.assertEqual(refining.Recipe(self.SMELTING, "steel_ingot", 3, 10).gold_cost, 450)
        self.assertEqual(
            refining.Recipe(self.SMELTING, "copper_ingot", 1, 6).duration_seconds, 60)

    def test_inputs_enumerated_completely(self):
        recipe = refining.Recipe(self.SMELTING, "iron_ingot", 2, cycles=3)
        self.assertEqual(recipe.inputs(), {
            "iron_ore": 9, "smelting_flux_2": 3, "copper_ingot": 3})

    def test_missing_reports_every_shortfall(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=5)
        short = recipe.missing({"copper_ore": 40, "smelting_flux_1": 2}, gold=0)
        self.assertEqual(short["copper_ore"], 5)
        self.assertEqual(short["smelting_flux_1"], 3)
        self.assertEqual(short["gold"], 25)

    def test_missing_is_empty_when_affordable(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=2)
        self.assertEqual(
            recipe.missing({"copper_ore": 18, "smelting_flux_1": 2}, gold=10), {})

    def test_payload_matches_the_frontend_shape(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=7)
        self.assertEqual(recipe.payload(), {
            "workshopId": "smelting", "itemId": "copper_ingot", "cycles": 7})


class MaxCyclesTests(unittest.TestCase):
    SMELTING = refining.WORKSHOPS["smelting"]

    def test_limited_by_raw_material(self):
        cycles = refining.max_cycles(
            self.SMELTING, "copper_ingot",
            {"copper_ore": 27, "smelting_flux_1": 99}, gold=10**6)
        self.assertEqual(cycles, 3)

    def test_limited_by_catalyst(self):
        cycles = refining.max_cycles(
            self.SMELTING, "copper_ingot",
            {"copper_ore": 900, "smelting_flux_1": 4}, gold=10**6)
        self.assertEqual(cycles, 4)

    def test_limited_by_gold(self):
        cycles = refining.max_cycles(
            self.SMELTING, "copper_ingot",
            {"copper_ore": 900, "smelting_flux_1": 99}, gold=27)
        self.assertEqual(cycles, 5)

    def test_limited_by_previous_tier_for_higher_recipes(self):
        cycles = refining.max_cycles(
            self.SMELTING, "iron_ingot",
            {"iron_ore": 900, "smelting_flux_2": 99, "copper_ingot": 2}, gold=10**6)
        self.assertEqual(cycles, 2)

    def test_zero_without_a_catalyst(self):
        self.assertEqual(refining.max_cycles(
            self.SMELTING, "copper_ingot", {"copper_ore": 900}, gold=10**6), 0)

    def test_unknown_item_yields_nothing(self):
        self.assertEqual(refining.max_cycles(
            self.SMELTING, "mystery", {"copper_ore": 900}, gold=10**6), 0)


class BestRecipeTests(unittest.TestCase):
    SMELTING = refining.WORKSHOPS["smelting"]
    HOLDINGS = {"copper_ore": 900, "iron_ore": 900,
                "smelting_flux_1": 50, "smelting_flux_2": 50, "copper_ingot": 50}

    def test_grade_caps_the_tier_considered(self):
        recipe = refining.best_recipe(
            self.SMELTING, level=99, grade=1, holdings=self.HOLDINGS, gold=10**6,
            market=bids(copper_ingot=888, iron_ingot=1200))
        self.assertEqual(recipe.tier, 1)

    def test_higher_grade_reaches_the_more_valuable_tier(self):
        recipe = refining.best_recipe(
            self.SMELTING, level=99, grade=2, holdings=self.HOLDINGS, gold=10**6,
            market=bids(copper_ingot=10, iron_ingot=5000))
        self.assertEqual(recipe.item_id, "iron_ingot")

    def test_prefers_total_value_not_unit_price(self):
        # copper can run far more cycles, so it wins despite the lower unit price.
        recipe = refining.best_recipe(
            self.SMELTING, level=99, grade=2,
            holdings={"copper_ore": 900, "smelting_flux_1": 50,
                      "iron_ore": 9, "smelting_flux_2": 1, "copper_ingot": 1},
            gold=10**6, market=bids(copper_ingot=500, iron_ingot=900))
        self.assertEqual(recipe.item_id, "copper_ingot")

    def test_returns_none_without_materials(self):
        self.assertIsNone(refining.best_recipe(
            self.SMELTING, level=99, grade=7, holdings={}, gold=10**6, market=None))


class ScoringTests(unittest.TestCase):
    SMELTING = refining.WORKSHOPS["smelting"]

    def test_refining_turns_worthless_ore_into_a_priced_output(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=10)
        candidate = econ.refining_candidate(recipe, bids(copper_ingot=888), Config())
        self.assertEqual(candidate.gold_equivalent, 8880)
        self.assertEqual(candidate.gold_cost, 50)
        self.assertEqual(candidate.energy_cost, 0, "refining spends no energy")
        self.assertFalse(candidate.degraded)

    def test_unpriced_output_scores_zero_and_is_flagged(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=10)
        candidate = econ.refining_candidate(recipe, build_snapshot([]), Config())
        self.assertEqual(candidate.gold_equivalent, 0)
        self.assertTrue(candidate.degraded)
        self.assertIn("no bid", candidate.reason)

    def test_reason_lists_the_inputs_consumed(self):
        recipe = refining.Recipe(self.SMELTING, "copper_ingot", 1, cycles=2)
        reason = econ.refining_candidate(recipe, bids(copper_ingot=888), Config()).reason
        self.assertIn("18× copper_ore", reason)
        self.assertIn("2× smelting_flux_1", reason)


class OrchestratorTests(unittest.TestCase):
    HOLDINGS = {"copper_ore": 90, "smelting_flux_1": 10}

    def _state(self, **overrides):
        doc = {"level": 6, "grade": 1, "energy": 80, "maxEnergy": 100,
               "balance": 5000, "currentHealth": 130, "currentMana": 130,
               "currentLocationId": "city_1", "attributePoints": 0,
               "attributes": {"vitality": 3, "wisdom": 3},
               # Entry paid: the workshop is behind the city gate, and buying
               # that door is its own decision — see CityGateTests below.
               "cityAccessPasses": {"1": 4_102_444_800},
               "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None}
        doc.update(overrides)
        return parse_player(doc)

    def test_refines_when_standing_in_the_workshop_city(self):
        api = FakeApi()
        api.start_refining = lambda session, payload: api._record("startRefining", **payload)
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, self._state(), bids(copper_ingot=888), self.HOLDINGS)
        self.assertEqual(decision.action, "startRefining")
        self.assertEqual(decision.params["workshopId"], "smelting")
        self.assertEqual(decision.params["cycles"], 10)

    def test_no_refining_outside_a_workshop_city(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state(currentLocationId="city_2"), bids(copper_ingot=888), self.HOLDINGS)
        self.assertNotIn("startRefining", [c.action for c in candidates])

    def test_unprofitable_refining_is_rejected(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state(), build_snapshot([]), self.HOLDINGS)
        self.assertNotIn("startRefining", [c.action for c in candidates])

    def test_gold_reserve_is_respected(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False,
                                          gold_reserve=4990), api=FakeApi())
        candidates = orchestrator.build_candidates(
            self._state(balance=5000), bids(copper_ingot=888), self.HOLDINGS)
        for candidate in candidates:
            self.assertLessEqual(candidate.gold_cost, 10)

    def test_missing_holdings_do_not_crash_the_cycle(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        orchestrator.build_candidates(self._state(), bids(copper_ingot=888), None)


class FreeEnergyTests(unittest.TestCase):
    def _state(self, energy, used=0, date="2026-08-16"):
        return parse_player({
            "level": 6, "grade": 1, "energy": energy, "maxEnergy": 100,
            "balance": 100, "currentHealth": 130, "currentMana": 130,
            "currentLocationId": "city_2", "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": used, "lastFreeEnergyRefillDate": date})

    def test_three_refills_available_per_day(self):
        state = self._state(10, used=0, date="2026-08-16")
        self.assertEqual(state.free_refills_left("2026-08-16"), 3)

    def test_counter_decrements_with_use(self):
        self.assertEqual(self._state(10, used=2).free_refills_left("2026-08-16"), 1)

    def test_exhausted_quota_reports_zero(self):
        self.assertEqual(self._state(10, used=3).free_refills_left("2026-08-16"), 0)

    def test_counter_resets_on_a_new_day(self):
        state = self._state(10, used=3, date="2026-08-15")
        self.assertEqual(state.free_refills_left("2026-08-16"), 3)

    def test_refill_not_wasted_on_a_nearly_full_bar(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(self._state(95))
        self.assertNotIn("refillEnergyFree", [c.action for c in candidates])

    def test_refill_waits_for_a_bar_a_battle_can_still_spend(self):
        """A refill fills to the brim, so every point left in the bar is thrown
        away by calling it early. Measured on 2026-08-28: the old 35% floor
        spent three refills at 35 energy each and gave the fleet 295 energy a
        day where 400 was available. A battle costs one energy, and all fifty
        wallets were observed sitting at exactly zero, so the bar does drain.
        """
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        candidates = orchestrator.build_candidates(self._state(35))
        self.assertNotIn("refillEnergyFree", [c.action for c in candidates])

    def test_refill_taken_once_the_bar_is_spent(self):
        api = FakeApi()
        api.refill_energy_free = lambda session: api._record("refillEnergyFree")
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        decision = orchestrator.decide_and_act({"id": "w1"}, None, self._state(0))
        self.assertEqual(decision.action, "refillEnergyFree")

    def test_no_refill_once_the_daily_quota_is_gone(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        candidates = orchestrator.build_candidates(self._state(5, used=3, date=today))
        self.assertNotIn("refillEnergyFree", [c.action for c in candidates])


if __name__ == "__main__":
    unittest.main()


class CatalystTests(unittest.TestCase):
    SMELTING = refining.WORKSHOPS["smelting"]

    def test_prices_follow_the_shop_table(self):
        self.assertEqual(refining.catalyst_price(1), 20)
        self.assertEqual(refining.catalyst_price(7), 14580)
        self.assertEqual(refining.catalyst_price(99), 0)

    def test_each_workshop_knows_its_shop(self):
        shops = {w.shop_id for w in refining.WORKSHOPS.values()}
        self.assertEqual(shops, {"flux_shop", "oil_shop", "salt_shop", "fixative_shop"})

    def test_payload_matches_the_frontend_shape(self):
        self.assertEqual(refining.catalyst_payload(self.SMELTING, 1, 5),
                         {"shopId": "flux_shop", "tier": 1, "quantity": 5})

    def test_purchase_capped_by_gold_and_by_need(self):
        self.assertEqual(refining.affordable_catalysts(1, gold=100, wanted=10), 5)
        self.assertEqual(refining.affordable_catalysts(1, gold=1000, wanted=3), 3)
        self.assertEqual(refining.affordable_catalysts(1, gold=10, wanted=10), 0)

    def test_reachable_cycles_account_for_buying_catalysts(self):
        # 90 ore covers 10 cycles; gold covers (20 + 5) each, so 250 gold caps at 10.
        self.assertEqual(refining.cycles_if_catalyst_bought(
            self.SMELTING, "copper_ingot", {"copper_ore": 90}, gold=250), 10)
        self.assertEqual(refining.cycles_if_catalyst_bought(
            self.SMELTING, "copper_ingot", {"copper_ore": 90}, gold=100), 4)

    def test_reachable_cycles_still_limited_by_raw_material(self):
        self.assertEqual(refining.cycles_if_catalyst_bought(
            self.SMELTING, "copper_ingot", {"copper_ore": 18}, gold=10**6), 2)

    def test_orchestrator_buys_catalysts_when_they_are_the_blocker(self):
        api = FakeApi()
        api.purchase_crafting_item = lambda s, p: api._record("purchaseCraftingItem", **p)
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=api)
        state = parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_1",
            "attributes": {"vitality": 3, "wisdom": 3},
            "cityAccessPasses": {"1": 4_102_444_800},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None})
        # Raw material in hand, zero catalysts — exactly the blocked case.
        decision = orchestrator.decide_and_act(
            {"id": "w1"}, None, state, bids(copper_ingot=888), {"copper_ore": 90})
        self.assertEqual(decision.action, "purchaseCraftingItem")
        self.assertEqual(decision.params["shopId"], "flux_shop")
        self.assertEqual(decision.params["tier"], 1)

    def test_no_catalyst_purchase_without_raw_material(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        state = parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_1",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None})
        candidates = orchestrator.build_candidates(state, bids(copper_ingot=888), {})
        self.assertNotIn("purchaseCraftingItem", [c.action for c in candidates])

    def test_no_catalyst_purchase_when_the_output_has_no_bid(self):
        orchestrator = make(config=Config(enabled=True, dry_run=False), api=FakeApi())
        state = parse_player({
            "level": 6, "grade": 1, "energy": 80, "maxEnergy": 100, "balance": 5000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "city_1",
            "attributes": {"vitality": 3, "wisdom": 3},
            "claimedInitialRewardsV2": list(range(1, 7)), "newbieQuest": 999, "activity": None})
        candidates = orchestrator.build_candidates(
            state, build_snapshot([]), {"copper_ore": 90})
        self.assertNotIn("purchaseCraftingItem", [c.action for c in candidates])

    def test_full_chain_is_profitable_at_measured_prices(self):
        """9 ore + 1 catalyst + 5 gold against an 888 bid."""
        from slcw import farming
        per_unit = farming.gold_mode_cost(1, 8)["gold"] / (60 * 8)
        cost = per_unit * 9 + refining.catalyst_price(1) + refining.GOLD_PER_CYCLE[1]
        self.assertLess(cost, 100)
        self.assertGreater(888 / cost, 10, "chain should return over 10x")


class ChainValuationTests(unittest.TestCase):
    """Raw materials are not traded; their worth is the refined good they feed."""

    def test_raw_ore_is_valued_through_its_ingot(self):
        # 9 ore + 1 catalyst (20g) + 5g refine -> 1 copper_ingot at 888g.
        value = refining.raw_material_value("copper_ore", bids(copper_ingot=888))
        self.assertAlmostEqual(value, (888 - 20 - 5) / 9, places=4)

    def test_zero_when_the_refined_output_has_no_bid_either(self):
        self.assertEqual(refining.raw_material_value("copper_ore", build_snapshot([])), 0.0)

    def test_zero_when_the_chain_would_lose_money(self):
        # A 10g ingot cannot repay a 20g catalyst plus 5g of refining.
        self.assertEqual(refining.raw_material_value("copper_ore", bids(copper_ingot=10)), 0.0)

    def test_grade_caps_which_tier_the_value_may_come_from(self):
        market = bids(iron_ingot=100000)
        self.assertEqual(refining.raw_material_value("iron_ore", market, grade=1), 0.0)
        self.assertGreater(refining.raw_material_value("iron_ore", market, grade=2), 0)

    def test_unknown_material_is_worthless(self):
        self.assertEqual(refining.raw_material_value("moon_rock", bids(copper_ingot=888)), 0.0)

    def test_missing_market_is_worthless(self):
        self.assertEqual(refining.raw_material_value("copper_ore", None), 0.0)

    def test_gathering_becomes_viable_once_the_chain_is_priced(self):
        """The bug this fixes: an 11,000-gold wallet still refused to gather.

        Behind SLCW_REFINING_CHAIN_PROVEN now: the value is real arithmetic,
        but it is four steps away, and this fleet has never walked them —
        157 farming runs paid 11,772 gold and one black-market order was ever
        filled. The switch is what says the chain has been seen to work.
        """
        from slcw.config import Config
        from slcw.model import parse_player
        from slcw.orchestrator import _GoldBudget
        from slcw import farming as farm_mod

        state = parse_player({
            "level": 6, "grade": 1, "energy": 90, "maxEnergy": 100,
            "balance": 11000, "currentHealth": 130, "currentMana": 130,
            "attributes": {"vitality": 3, "wisdom": 3}})
        resource = farm_mod.resources_at("farm_2")[0]          # copper_ore
        budget = _GoldBudget(state, 10500)

        proven = Config(refining_chain_proven=True)
        blind = econ.farming_candidates(resource, budget, build_snapshot([]), proven)
        priced = econ.farming_candidates(resource, budget, bids(copper_ingot=888), proven)
        unproven = econ.farming_candidates(
            resource, budget, bids(copper_ingot=888), Config())
        self.assertTrue(all(c.gold_equivalent == 0 for c in unproven),
                        "an unproven chain must not price the raw material")

        self.assertTrue(all(c.gold_equivalent == 0 for c in blind))
        self.assertTrue(any(c.gold_equivalent > 0 for c in priced))
        self.assertTrue(any("via refining" in c.reason for c in priced))

    def test_gold_budget_passes_grade_through(self):
        from slcw.model import parse_player
        from slcw.orchestrator import _GoldBudget
        state = parse_player({"grade": 3, "energy": 50,
                              "attributes": {"vitality": 3, "wisdom": 3}})
        self.assertEqual(_GoldBudget(state, 100).grade, 3)


class CityGateTests(unittest.TestCase):
    """The workshop and its shop are behind a gate, and the gate is a purchase.

    wallet-33 found this on 2026-08-28: it walked to city_13 for tanning salt,
    was refused three times with PERMISSION_DENIED "City access pass expired or
    required", and the circuit breaker paused it. That refusal is the reason
    the whole gather-refine-sell chain had produced one filled order in the
    fleet's lifetime.
    """

    def _state(self, **overrides):
        doc = {"level": 6, "grade": 1, "energy": 80, "maxEnergy": 100,
               "balance": 5000, "currentHealth": 130, "currentMana": 130,
               "currentLocationId": "city_1", "attributePoints": 0,
               "attributes": {"vitality": 3, "wisdom": 3},
               "claimedInitialRewardsV2": list(range(1, 7)),
               "newbieQuest": 999, "activity": None}
        doc.update(overrides)
        return parse_player(doc)

    def test_the_gate_is_bought_before_the_catalyst(self):
        from tests.test_orchestrator import make
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        top = orchestrator.build_candidates(
            self._state(), bids(copper_ingot=888), {"copper_ore": 90},
            wallet_id="w1")[0]
        self.assertEqual(top.action, "payCityEntryFee")
        self.assertEqual(top.params["cityId"], "1")

    def test_a_paid_gate_is_not_bought_twice(self):
        from tests.test_orchestrator import make
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        state = self._state(cityAccessPasses={"1": 4_102_444_800})
        actions = [c.action for c in orchestrator.build_candidates(
            state, bids(copper_ingot=888), {"copper_ore": 90}, wallet_id="w1")]
        self.assertNotIn("payCityEntryFee", actions)

    def test_no_gate_is_bought_where_there_is_no_workshop(self):
        from tests.test_orchestrator import make
        orchestrator = make(config=Config(enabled=True, dry_run=True), api=FakeApi())
        actions = [c.action for c in orchestrator.build_candidates(
            self._state(currentLocationId="farm_3"), bids(copper_ingot=888),
            {"copper_ore": 90}, wallet_id="w1")]
        self.assertNotIn("payCityEntryFee", actions)
