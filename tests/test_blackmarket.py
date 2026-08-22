import unittest

from slcw import blackmarket
from slcw.market import build_snapshot


def market(**bids):
    """A Black Market with these buy-side prices and plenty of depth."""
    return build_snapshot([
        {"status": "open", "type": "buy", "templateId": item,
         "price": price, "quantity": 9_999, "filled": 0}
        for item, price in bids.items()])


class RefinedGoodsTests(unittest.TestCase):
    """What the Black Market actually buys.

    Measured on 2026-08-22 by pulling all 6,000 open orders: the buy side is
    refined goods and nothing else. Raw ore, logs, hides and monster drops have
    no bid anywhere, on either market — 3,495 player-market orders contained
    three distinct items, none of them anything the fleet holds.
    """

    def test_an_ingot_is_refined(self):
        self.assertTrue(blackmarket.is_refined("copper_ingot"))
        self.assertTrue(blackmarket.is_refined("mithril_ingot"))

    def test_planks_leather_and_cloth_count_too(self):
        for item in ("pine_plank", "scale_leather", "linen_cloth"):
            self.assertTrue(blackmarket.is_refined(item), item)

    def test_the_ore_it_was_made_from_does_not(self):
        self.assertFalse(blackmarket.is_refined("copper_ore"))
        self.assertFalse(blackmarket.is_refined("trollblood"))


class SaleChoiceTests(unittest.TestCase):
    """Picking what to sell, and how much of it."""

    def test_the_most_valuable_stack_goes_first(self):
        sale = blackmarket.next_sale(
            {"copper_ingot": 40, "mithril_ingot": 40},
            market(copper_ingot=899, mithril_ingot=3_300))
        self.assertEqual(sale.item, "mithril_ingot")

    def test_a_reserve_is_left_for_the_crafting_bench(self):
        """Crafting turns these into gear the shop pays thousands for, so the
        seller must not empty the shelf it feeds."""
        sale = blackmarket.next_sale(
            {"copper_ingot": 30}, market(copper_ingot=899))
        self.assertEqual(sale.quantity, 30 - blackmarket.CRAFTING_RESERVE)

    def test_a_stack_at_or_below_the_reserve_is_left_alone(self):
        self.assertIsNone(blackmarket.next_sale(
            {"copper_ingot": blackmarket.CRAFTING_RESERVE},
            market(copper_ingot=899)))

    def test_nothing_is_sold_without_a_bid(self):
        self.assertIsNone(blackmarket.next_sale({"copper_ingot": 99}, market()))

    def test_raw_material_is_never_offered(self):
        self.assertIsNone(blackmarket.next_sale(
            {"copper_ore": 999}, market(copper_ore=500)))

    def test_a_sale_worth_less_than_the_cycle_is_skipped(self):
        self.assertIsNone(blackmarket.next_sale(
            {"linen_cloth": 30}, market(linen_cloth=1)))

    def test_an_empty_bag_sells_nothing(self):
        self.assertIsNone(blackmarket.next_sale({}, market(copper_ingot=899)))

    def test_no_market_at_all_sells_nothing(self):
        self.assertIsNone(blackmarket.next_sale({"copper_ingot": 99}, None))


class ProceedsTests(unittest.TestCase):
    """The Black Market takes a fifth.

    Measured: five copper_ingot returned totalGold 4,495 with tax 899, and the
    wallet's balance rose by 3,596. So the quoted bid is gross and a fifth of it
    does not arrive.
    """

    def test_the_tax_is_taken_off_the_quote(self):
        self.assertEqual(blackmarket.net_proceeds(4_495), 3_596)

    def test_a_sale_is_valued_net_not_gross(self):
        sale = blackmarket.next_sale(
            {"copper_ingot": 30}, market(copper_ingot=899))
        self.assertEqual(sale.gross, 899 * (30 - blackmarket.CRAFTING_RESERVE))
        self.assertEqual(sale.net, blackmarket.net_proceeds(sale.gross))


class DecisionTests(unittest.TestCase):
    """The sale as the decision loop drives it."""

    def _state(self, **over):
        from slcw.model import parse_player
        doc = {
            "level": 18, "grade": 2, "energy": 80, "maxEnergy": 100,
            "balance": 30_000, "currentHealth": 200, "maxHealth": 200,
            "currentMana": 130, "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3}, "attributePoints": 0,
            "claimedInitialRewardsV2": list(range(1, 19)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
        }
        doc.update(over)
        return parse_player(doc)

    def _top(self, holdings, orch=None):
        from tests.test_orchestrator import make
        cands = (orch or make()).build_candidates(
            self._state(), market=market(copper_ingot=899, mithril_ingot=3_300),
            holdings=holdings, include_travel=False, wallet_id="w1")
        return cands[0] if cands else None

    def test_a_surplus_of_ingots_is_sold(self):
        top = self._top({"copper_ingot": 40})
        self.assertEqual(top.action, "executeBlackMarketOrder")
        self.assertEqual(top.params, {"resourceId": "copper_ingot",
                                      "action": "sell", "quantity": 30})

    def test_the_reason_says_what_it_is_worth(self):
        self.assertIn("gold", self._top({"copper_ingot": 40}).reason)

    def test_raw_material_alone_is_not_a_sale(self):
        top = self._top({"copper_ore": 900, "trollblood": 900})
        self.assertNotEqual(getattr(top, "action", None), "executeBlackMarketOrder")

    def test_the_call_reaches_the_api(self):
        from tests.test_orchestrator import make, FakeApi
        api = FakeApi()
        orch = make(api=api)
        orch.decide_and_act({"id": "w1"}, None, self._state(),
                            market=market(copper_ingot=899),
                            holdings={"copper_ingot": 40})
        self.assertIn("executeBlackMarketOrder", [c[0] for c in api.calls])

    def test_a_refusal_parks_the_item(self):
        from tests.test_orchestrator import make, FakeApi
        api = FakeApi()
        api.fail_with = ("executeBlackMarketOrder", "FAILED_PRECONDITION",
                         "Insufficient liquidity")
        orch = make(api=api)
        orch.decide_and_act({"id": "w1"}, None, self._state(),
                            market=market(copper_ingot=899),
                            holdings={"copper_ingot": 40})
        self.assertTrue(orch.rejections.is_parked(
            "w1", "executeBlackMarketOrder",
            {"resourceId": "copper_ingot", "action": "sell", "quantity": 30}))


class LedgerTests(unittest.TestCase):
    def test_the_proceeds_are_recorded_net(self):
        from slcw import ledger
        summary = ledger._extract_summary("executeBlackMarketOrder", {
            "success": True, "totalFilled": 5, "totalGold": 4_495, "tax": 899})
        self.assertEqual(summary["gold"], 3_596)
        self.assertEqual(summary["quantity"], 5)

    def test_a_reply_with_nothing_filled_records_nothing(self):
        from slcw import ledger
        self.assertEqual(
            ledger._extract_summary("executeBlackMarketOrder", {"success": True}), {})
