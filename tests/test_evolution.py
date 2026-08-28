import unittest

from slcw import evolution


class RequirementTests(unittest.TestCase):
    """The table, read verbatim from app/evolution's own bundle.

    Levels run 15 per grade and seals run five-fold per grade, but both are
    written out rather than computed: a formula that happens to fit six rows is
    a guess about the seventh, and this one is spent irreversibly.
    """

    def test_the_measured_table(self):
        self.assertEqual(evolution.requirement(2), (15, 5))
        self.assertEqual(evolution.requirement(3), (30, 25))
        self.assertEqual(evolution.requirement(4), (45, 125))
        self.assertEqual(evolution.requirement(5), (60, 625))
        self.assertEqual(evolution.requirement(6), (75, 3125))
        self.assertEqual(evolution.requirement(7), (90, 15625))

    def test_there_is_no_grade_eight(self):
        self.assertIsNone(evolution.requirement(8))

    def test_grade_one_asks_for_nothing(self):
        self.assertEqual(evolution.requirement(1), (0, 0))


class ReadinessTests(unittest.TestCase):
    def test_a_level_fifteen_wallet_with_five_seals_may_ascend(self):
        self.assertTrue(evolution.can_evolve(level=15, grade=1, seals=5))

    def test_four_seals_is_not_five(self):
        self.assertFalse(evolution.can_evolve(level=15, grade=1, seals=4))

    def test_the_level_gate_is_real_too(self):
        self.assertFalse(evolution.can_evolve(level=14, grade=1, seals=99))

    def test_the_top_grade_has_nowhere_to_go(self):
        self.assertFalse(evolution.can_evolve(level=99, grade=7, seals=99_999))

    def test_seals_still_to_find(self):
        self.assertEqual(evolution.seals_needed(grade=1, held=4), 1)
        self.assertEqual(evolution.seals_needed(grade=1, held=0), 5)
        self.assertEqual(evolution.seals_needed(grade=1, held=9), 0)
        self.assertEqual(evolution.seals_needed(grade=7, held=0), 0)


class PriceTests(unittest.TestCase):
    """The shop's price moves with how full the city warehouse is.

    Read from the imperial shop bundle: base 3,500 gold, doubling as the
    warehouse empties and halving as it fills, so the real range is 1,750 to
    7,000. Greyholm held 10,148 of 15,000 on 2026-08-22, which prices a seal at
    2,883 — against a fleet that earns 161,000 gold an hour.
    """

    def test_the_measured_price_at_greyholm(self):
        self.assertEqual(evolution.seal_price(stock=10_148, capacity=15_000), 2_883)

    def test_an_empty_warehouse_charges_double(self):
        self.assertEqual(evolution.seal_price(stock=0, capacity=15_000), 7_000)

    def test_a_full_warehouse_charges_half(self):
        self.assertEqual(evolution.seal_price(stock=15_000, capacity=15_000), 1_750)

    def test_the_halfway_mark_is_the_base_price(self):
        self.assertEqual(evolution.seal_price(stock=7_500, capacity=15_000), 3_500)

    def test_an_unknown_warehouse_quotes_the_base_rather_than_guessing(self):
        """Capacity zero means the city document was not read, not that the
        warehouse is empty — and reading it as empty would quote double."""
        self.assertEqual(evolution.seal_price(stock=0, capacity=0), 3_500)


class DecisionTests(unittest.TestCase):
    """Ascending, as the decision loop drives it.

    A wallet at its grade cap is throwing every point of XP it earns away, so
    this outranks the things that earn XP. It runs only when the level gate is
    already met, which is what stops it dragging a wallet across the map for
    something it cannot do when it arrives.
    """

    def _state(self, **over):
        from slcw.model import parse_player
        doc = {
            "level": 15, "energy": 80, "maxEnergy": 100, "balance": 50_000,
            "currentHealth": 130, "currentMana": 130, "grade": 1,
            "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 16)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
            "cityAccessPasses": {"17": 4_102_444_800},
        }
        doc.update(over)
        return parse_player(doc)

    def _top(self, holdings=None, orch=None, **over):
        from tests.test_orchestrator import make
        orch = orch or make()
        cands = orch.build_candidates(
            self._state(**over), holdings=holdings if holdings is not None else {},
            include_travel=False, wallet_id="w1")
        return cands[0] if cands else None

    def test_a_capped_wallet_heads_for_greyholm(self):
        top = self._top(holdings={"imperial_seal": 5})
        self.assertEqual(top.action, "startTravel")
        self.assertEqual(top.params["destinationId"], "city_17")

    def test_standing_at_the_altar_with_five_seals_it_ascends(self):
        top = self._top(holdings={"imperial_seal": 5}, currentLocationId="city_17")
        self.assertEqual(top.action, "evolveGrade")

    def test_one_seal_short_it_buys_the_difference(self):
        top = self._top(holdings={"imperial_seal": 4}, currentLocationId="city_17")
        self.assertEqual(top.action, "purchaseImperialSeal")
        self.assertEqual(top.params, {"quantity": 1})

    def test_with_no_seals_at_all_it_buys_all_five(self):
        top = self._top(holdings={}, currentLocationId="city_17")
        self.assertEqual(top.params, {"quantity": 5})

    def test_it_pays_the_entry_fee_before_shopping(self):
        top = self._top(holdings={"imperial_seal": 4}, currentLocationId="city_17",
                        cityAccessPasses={})
        self.assertEqual(top.action, "payCityEntryFee")
        self.assertEqual(top.params, {"cityId": "17"})

    def test_a_citizen_needs_no_entry_fee(self):
        top = self._top(holdings={"imperial_seal": 5}, currentLocationId="city_17",
                        cityAccessPasses={}, citizenship={"17": True})
        self.assertEqual(top.action, "evolveGrade")

    def test_a_wallet_below_the_cap_is_left_to_play(self):
        """Nothing is being wasted yet, and the seals keep."""
        top = self._top(holdings={"imperial_seal": 5}, level=12)
        self.assertNotEqual(top.action, "startTravel")

    def test_a_wallet_that_cannot_afford_the_seals_does_not_try(self):
        """And at the ceiling with no gold there may be nothing to do at all —
        experience is discarded there, so an ordinary fight scores nothing
        either."""
        top = self._top(holdings={}, currentLocationId="city_17", balance=100)
        self.assertNotEqual(getattr(top, "action", None), "purchaseImperialSeal")

    def test_the_top_grade_is_left_alone(self):
        top = self._top(holdings={"imperial_seal": 99_999},
                        grade=7, level=105, currentLocationId="city_17")
        self.assertNotEqual(top.action, "evolveGrade")

    def test_a_refused_purchase_is_parked_rather_than_repeated(self):
        from tests.test_orchestrator import make, FakeApi
        api = FakeApi()
        api.fail_with = ("purchaseImperialSeal", "FAILED_PRECONDITION",
                         "Not enough gold")
        orch = make(api=api)
        state = self._state(currentLocationId="city_17")
        orch.decide_and_act({"id": "w1"}, None, state, holdings={"imperial_seal": 4})
        self.assertTrue(orch.rejections.is_parked(
            "w1", "purchaseImperialSeal", {"quantity": 1}))


class ContinuityTests(unittest.TestCase):
    """It does not stop at grade 2.

    Nothing in the branch is written for a particular grade: it reads the next
    one out of the table each time. These pin that down, because the whole
    point of raising a grade is that the next ceiling arrives soon after.
    """

    def _state(self, level, grade, **over):
        from slcw.model import parse_player
        doc = {
            "level": level, "grade": grade, "energy": 80, "maxEnergy": 100,
            "balance": 300_000, "currentHealth": 200, "maxHealth": 200,
            "currentMana": 130, "currentLocationId": "city_17",
            "attributes": {"wisdom": 3, "vitality": 3}, "attributePoints": 0,
            "claimedInitialRewardsV2": list(range(1, level + 1)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
            "cityAccessPasses": {"17": 4_102_444_800},
        }
        doc.update(over)
        return parse_player(doc)

    def _top(self, level, grade, seals, **over):
        from tests.test_orchestrator import make
        cands = make().build_candidates(
            self._state(level, grade, **over),
            holdings={"imperial_seal": seals},
            include_travel=False, wallet_id="w1")
        return cands[0] if cands else None

    def test_a_grade_two_wallet_at_its_own_ceiling_goes_again(self):
        top = self._top(level=30, grade=2, seals=25)
        self.assertEqual(top.action, "evolveGrade")

    def test_it_buys_the_twenty_five_seals_grade_three_wants_a_batch_at_a_time(self):
        """Only a single-seal purchase has ever been measured, so twenty-five
        are committed five at a time rather than in one call the shop might
        cap, misprice, or refuse whole."""
        top = self._top(level=30, grade=2, seals=0)
        self.assertEqual(top.action, "purchaseImperialSeal")
        self.assertEqual(top.params, {"quantity": 5})

    def test_the_last_batch_is_only_as_big_as_what_is_missing(self):
        top = self._top(level=30, grade=2, seals=23)
        self.assertEqual(top.params, {"quantity": 2})

    def test_a_grade_two_wallet_below_thirty_is_left_to_level(self):
        self.assertNotEqual(self._top(level=25, grade=2, seals=25).action,
                            "evolveGrade")

    def test_the_ladder_runs_all_the_way_up(self):
        for grade, level, seals in ((3, 45, 125), (4, 60, 625),
                                    (5, 75, 3_125), (6, 90, 15_625)):
            top = self._top(level=level, grade=grade, seals=seals,
                            balance=999_999_999)
            self.assertEqual(top.action, "evolveGrade", f"grade {grade}")

    def test_grade_seven_is_the_end_of_it(self):
        self.assertNotEqual(getattr(self._top(level=105, grade=7, seals=99_999),
                                    "action", None), "evolveGrade")
