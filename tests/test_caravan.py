import unittest

from slcw import caravan


def workshop(output, held, needs=None, capacity=5000, tax=10):
    return {
        "warehouseCapacity": capacity,
        "taxRate": tax,
        "warehouse": {
            "output": {"templateId": output, "quantity": held},
            "input": [{"templateId": k, "quantity": v} for k, v in (needs or {}).items()],
        },
    }


def hub(holdings, capacity=50000, tax=10):
    return {
        "warehouseCapacity": capacity,
        "taxRate": tax,
        "warehouse": {
            "isTradeHub": True,
            "outputs": [{"templateId": k, "quantity": v} for k, v in holdings.items()],
            "input": [],
        },
    }


class PricingTests(unittest.TestCase):
    def test_an_empty_shelf_charges_twice_the_base(self):
        self.assertEqual(caravan.unit_price("runic_alloy", 0, 500), 9600)

    def test_a_half_full_shelf_charges_the_base(self):
        self.assertEqual(caravan.unit_price("runic_alloy", 250, 500), 4800)

    def test_a_full_shelf_charges_half(self):
        self.assertEqual(caravan.unit_price("runic_alloy", 500, 500), 2400)

    def test_the_discount_never_goes_below_half(self):
        self.assertEqual(caravan.unit_price("runic_alloy", 10_000, 500), 2400)

    def test_capacity_changes_where_the_turn_is(self):
        """A big warehouse has to be much emptier before it pays a premium."""
        self.assertGreater(caravan.unit_price("battle_ember", 5000, 50_000),
                           caravan.unit_price("battle_ember", 5000, 5_000))

    def test_an_unknown_good_is_worth_nothing(self):
        self.assertEqual(caravan.unit_price("trollblood", 0, 500), 0)

    def test_the_hub_pays_a_flat_eighty_percent(self):
        self.assertEqual(caravan.hub_flat_price("chronicle_page"), 3360)


class RoutingTests(unittest.TestCase):
    """The rule the server taught us: 'Caravans from cities must go to Hub.'"""

    def test_a_workshop_may_only_ship_to_the_hub(self):
        city = workshop("chronicle_page", 432, capacity=500)
        self.assertEqual(caravan.legal_destinations("city_14", city, "chronicle_page"),
                         ["city_2"])

    def test_city_to_city_is_never_offered(self):
        city = workshop("chronicle_page", 432, capacity=500)
        self.assertNotIn("city_17",
                         caravan.legal_destinations("city_14", city, "chronicle_page"))

    def test_the_hub_may_ship_to_every_consumer(self):
        self.assertEqual(
            caravan.legal_destinations("city_2", hub({"battle_ember": 49_935}),
                                       "battle_ember"),
            ["city_17"])

    def test_the_hub_never_ships_to_itself(self):
        self.assertNotIn("city_2",
                         caravan.legal_destinations("city_2", hub({"runic_alloy": 1}),
                                                    "runic_alloy"))


class LegTests(unittest.TestCase):
    def setUp(self):
        self.hub = hub({"battle_ember": 49_935, "runic_alloy": 49_953})
        self.greyholm = workshop("imperial_seal", 10_215,
                                 needs={"battle_ember": 5063, "chronicle_page": 0},
                                 capacity=15_000, tax=0)
        self.ostrim = workshop("chronicle_page", 418, capacity=500, tax=5)
        self.cities = {"city_2": self.hub, "city_17": self.greyholm, "city_14": self.ostrim}

    def test_the_hub_charges_a_flat_ask_however_much_it_is_holding(self):
        """Measured: 10 battle_ember out of a hub sitting on 49,935 of them
        cost 54,000, not the 22,500 its stock curve predicted."""
        leg = caravan.price_leg("city_2", self.hub, "city_17", self.greyholm,
                                "battle_ember", 10)
        self.assertEqual(leg.unit_cost, 5400)
        self.assertEqual(leg.cost, 54_000)

    def test_the_outbound_leg_is_paid_for_by_what_the_city_lacks(self):
        leg = caravan.price_leg("city_2", self.hub, "city_17", self.greyholm,
                                "battle_ember", 10)
        self.assertGreater(leg.unit_revenue, leg.unit_cost)
        self.assertEqual(leg.tax_rate, 0)
        self.assertGreater(leg.profit, 5_000)

    def test_a_starved_city_pays_far_more_than_a_stocked_one(self):
        starved = caravan.price_leg("city_2", self.hub, "city_17",
                                    workshop("imperial_seal", 1,
                                             needs={"battle_ember": 0},
                                             capacity=15_000, tax=0),
                                    "battle_ember", 10)
        stocked = caravan.price_leg("city_2", self.hub, "city_17", self.greyholm,
                                    "battle_ember", 10)
        self.assertGreater(starved.profit, stocked.profit)

    def test_the_return_leg_is_priced_flat(self):
        leg = caravan.price_leg("city_14", self.ostrim, "city_2", self.hub,
                                "chronicle_page", 10)
        self.assertEqual(leg.unit_revenue, caravan.hub_bid("chronicle_page"))

    def test_the_measured_return_leg_reproduces(self):
        """wallet-16, 2026-08-23: 10 chronicle_page bought at Ostrim on a shelf
        of 418, sold at Virtan for 33,600, keeping 5,710."""
        leg = caravan.price_leg("city_14", workshop("chronicle_page", 418,
                                                    capacity=500, tax=5),
                                "city_2", self.hub, "chronicle_page", 10)
        self.assertEqual(leg.unit_revenue * 10, 33_600)
        self.assertAlmostEqual(leg.profit, 5_710, delta=400)

    def test_selling_into_the_hub_is_not_taxed(self):
        """Virtan's taxRate is 10 and the measured leg kept the whole spread."""
        leg = caravan.price_leg("city_14", self.ostrim, "city_2",
                                dict(self.hub, taxRate=10), "chronicle_page", 10)
        self.assertEqual(leg.tax_rate, 0)
        self.assertEqual(leg.profit, leg.unit_revenue * 10 - leg.cost)

    def test_tax_is_taken_from_the_profit_and_not_the_principal(self):
        taxed = caravan.price_leg("city_2", self.hub, "city_17",
                                  dict(self.greyholm, taxRate=50),
                                  "battle_ember", 10)
        free = caravan.price_leg("city_2", self.hub, "city_17",
                                 dict(self.greyholm, taxRate=0),
                                 "battle_ember", 10)
        self.assertEqual(taxed.cost, free.cost)
        self.assertLess(taxed.profit, free.profit)

    def test_a_city_cannot_sell_what_it_does_not_make(self):
        self.assertIsNone(caravan.price_leg("city_14", self.ostrim, "city_2", self.hub,
                                            "runic_alloy", 10))

    def test_a_shelf_shorter_than_the_load_carries_nothing(self):
        thin = workshop("chronicle_page", 3, capacity=500)
        self.assertIsNone(caravan.price_leg("city_14", thin, "city_2", self.hub,
                                            "chronicle_page", 10))

    def test_travel_is_twenty_seconds_per_unit_of_distance(self):
        leg = caravan.price_leg("city_14", self.ostrim, "city_2", self.hub,
                                "chronicle_page", 10)
        self.assertEqual(leg.travel_seconds, caravan.travel_seconds("city_14", "city_2"))
        self.assertGreater(leg.travel_seconds, 900)


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.cities = {
            "city_2": hub({"battle_ember": 49_935, "runic_alloy": 49_953,
                           "aether_fragment": 1698}),
            "city_17": workshop("imperial_seal", 10_215,
                                needs={"battle_ember": 5063}, capacity=15_000, tax=0),
            "city_15": workshop("aqua_vitae", 4818, needs={"runic_alloy": 1781,
                                                           "aether_fragment": 1712}),
            "city_14": workshop("chronicle_page", 418, capacity=500, tax=5),
        }

    def test_the_best_load_out_of_the_hub_is_the_one_a_city_is_starved_of(self):
        leg = caravan.best_leg("city_2", self.cities, gold=1_000_000)
        self.assertEqual(leg.template_id, "battle_ember")
        self.assertEqual(leg.destination_id, "city_17")

    def test_a_load_that_would_lose_money_is_not_offered(self):
        """A city already swimming in the good pays half base, well under the
        hub's flat 1.2x ask, so that leg is a loss and must never be chosen."""
        cities = {
            "city_2": hub({"aether_fragment": 1698}),
            "city_15": workshop("aqua_vitae", 4818,
                                needs={"aether_fragment": 5000}, capacity=5000),
        }
        self.assertIsNone(caravan.best_leg("city_2", cities, gold=1_000_000))

    def test_a_thin_purse_shrinks_the_load_rather_than_skipping_it(self):
        rich = caravan.best_leg("city_2", self.cities, gold=1_000_000)
        poor = caravan.best_leg("city_2", self.cities, gold=9_000)
        self.assertIsNotNone(poor)
        self.assertLess(poor.quantity, rich.quantity)
        self.assertLessEqual(poor.cost, 9_000)

    def test_no_gold_means_no_trade(self):
        self.assertIsNone(caravan.best_leg("city_2", self.cities, gold=0))

    def test_a_workshop_still_finds_its_one_legal_leg(self):
        leg = caravan.best_leg("city_14", self.cities, gold=1_000_000)
        self.assertEqual(leg.destination_id, "city_2")
        self.assertEqual(leg.template_id, "chronicle_page")

    def test_the_best_place_to_stand_is_wherever_pays_most(self):
        """Not always the hub: once its ask turned out to be a flat 1.2x base,
        the return legs into it often beat the runs out of it."""
        where, leg = caravan.best_origin(self.cities, gold=1_000_000)
        self.assertIn(where, self.cities)
        self.assertGreater(leg.profit, 0)
        for city_id in self.cities:
            other = caravan.best_leg(city_id, self.cities, gold=1_000_000)
            if other is not None:
                self.assertLessEqual(other.profit, leg.profit)

    def test_an_unknown_city_offers_nothing(self):
        self.assertIsNone(caravan.best_leg("farm_3", self.cities, gold=1_000_000))


if __name__ == "__main__":
    unittest.main()
