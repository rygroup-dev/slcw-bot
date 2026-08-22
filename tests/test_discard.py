"""What may be destroyed, and — mostly — what may not.

Every test here that asserts `is None` is protecting a real item from a call
with no undo, so they matter more than the one that asserts a deletion happens.
"""
import unittest

from slcw import discard, inventory as inv


class FakeMarket:
    """Bids by item id. Anything absent has no bid at all."""

    def __init__(self, bids=None):
        self.bids = bids or {}

    def best_bid(self, item_id):
        return self.bids.get(item_id)


def bag(*items, max_slots=None):
    """items: (templateId, quantity[, instanceId])"""
    slots = []
    for index, item in enumerate(items):
        template, quantity = item[0], item[1]
        instance = item[2] if len(item) > 2 else None
        slots.append({"slotIndex": index, "templateId": template,
                      "quantity": quantity, "instanceId": instance})
    return inv.parse_inventory(
        {"maxSlots": max_slots if max_slots is not None else len(items),
         "slots": slots})


class ProtectionTests(unittest.TestCase):
    def test_equipment_is_never_worthless(self):
        """Gear sells back for thousands once the shop's stock rotates."""
        for item in ("plate_armor_t1", "two_handed_sword_t2", "staff_t7"):
            self.assertFalse(discard.is_worthless(item, FakeMarket()), item)

    def test_containers_are_never_worthless(self):
        for item in ("small_equip_chest", "medium_equip_chest"):
            self.assertFalse(discard.is_worthless(item, FakeMarket()), item)

    def test_imperial_seals_are_never_worthless(self):
        """Five of them buy a grade, which lifts a level cap that is otherwise
        throwing away every point of XP the wallet earns."""
        self.assertFalse(discard.is_worthless("imperial_seal", FakeMarket()))

    def test_refining_inputs_are_never_worthless(self):
        for item in ("copper_ore", "oak_log", "wool_fiber", "wolf_skin"):
            self.assertFalse(discard.is_worthless(item, FakeMarket()), item)

    def test_refined_goods_are_never_worthless(self):
        for item in ("copper_ingot", "mithril_ingot", "linen_cloth"):
            self.assertFalse(discard.is_worthless(item, FakeMarket()), item)

    def test_anything_with_a_bid_is_never_worthless(self):
        market = FakeMarket({"frogslime": 12})
        self.assertFalse(discard.is_worthless("frogslime", market))

    def test_a_missing_market_protects_everything(self):
        """Fail closed: no book means no proof, and no proof means no deletion."""
        self.assertFalse(discard.is_worthless("frogslime", None))

    def test_an_unused_unbid_monster_drop_is_worthless(self):
        self.assertTrue(discard.is_worthless("frogslime", FakeMarket()))


class NextDiscardTests(unittest.TestCase):
    def test_nothing_is_destroyed_while_a_slot_is_free(self):
        inventory = bag(("frogslime", 60), max_slots=40)
        self.assertIsNone(discard.next_discard(inventory, FakeMarket()))

    def test_a_full_bag_gives_up_its_smallest_junk_stack(self):
        """Smallest first: the fewest items lost per slot recovered, and the
        big stacks — the ones a clan quest could finish — go last."""
        inventory = bag(("frogslime", 61), ("spiderfang", 4), ("giantbone", 23))
        chosen = discard.next_discard(inventory, FakeMarket())
        self.assertEqual(chosen.item_id, "spiderfang")
        self.assertEqual(chosen.quantity, 4)
        self.assertEqual(chosen.slot_index, 1)

    def test_a_stale_market_destroys_nothing(self):
        inventory = bag(("frogslime", 61), ("spiderfang", 4))
        self.assertIsNone(discard.next_discard(inventory, None))

    def test_the_active_clan_quest_outranks_a_full_bag(self):
        inventory = bag(("spiderfang", 4), ("frogslime", 61))
        chosen = discard.next_discard(
            inventory, FakeMarket(), quest_items=("spiderfang",))
        self.assertEqual(chosen.item_id, "frogslime")

    def test_a_bag_full_of_protected_items_destroys_nothing(self):
        inventory = bag(("plate_armor_t1", 1, "i1"), ("copper_ore", 90),
                        ("small_equip_chest", 3))
        self.assertIsNone(discard.next_discard(inventory, FakeMarket()))

    def test_gear_is_skipped_even_if_its_template_slipped_the_catalog(self):
        """An instance id means a real, individually-tracked piece of gear. It
        is what sellEquipmentItem takes, so it always has a way out that pays."""
        inventory = bag(("mystery_relic", 1, "i1"), ("frogslime", 61))
        chosen = discard.next_discard(inventory, FakeMarket())
        self.assertEqual(chosen.item_id, "frogslime")

    def test_real_fleet_holdings_lose_only_monster_drops(self):
        """The 2026-08-22 bag that started this, run to exhaustion: whatever is
        still standing at the end must be something the game can use."""
        held = [("frogslime", 61), ("livingwood", 59), ("spiderfang", 54),
                ("plate_armor_t1", 5, "i1"), ("medium_equip_chest", 4),
                ("imperial_seal", 2), ("copper_ingot", 30),
                ("two_handed_sword_t2", 1, "i2")]
        market = FakeMarket({"copper_ingot": 899})
        destroyed = []
        while True:
            inventory = bag(*held)
            chosen = discard.next_discard(inventory, market)
            if chosen is None:
                break
            destroyed.append(chosen.item_id)
            held = [h for h in held if h[0] != chosen.item_id]
        self.assertEqual(sorted(destroyed),
                         ["frogslime", "livingwood", "spiderfang"])
        self.assertEqual(
            sorted(h[0] for h in held),
            ["copper_ingot", "imperial_seal", "medium_equip_chest",
             "plate_armor_t1", "two_handed_sword_t2"])


class AuditTrailTests(unittest.TestCase):
    """A deletion is the one action with nothing to read back afterwards."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._real = discard.LOG_PATH
        discard.LOG_PATH = Path(tempfile.mkdtemp()) / "discards.jsonl"

    def tearDown(self):
        discard.LOG_PATH = self._real

    def test_a_destroyed_stack_is_written_down(self):
        discard.record("wallet-04", "frogslime", 61)
        discard.record("wallet-04", "aerocore", 1)
        discard.record("wallet-13", "frogslime", 2)
        self.assertEqual(discard.totals(), {"frogslime": 63, "aerocore": 1})

    def test_totals_are_empty_before_anything_is_destroyed(self):
        self.assertEqual(discard.totals(), {})

    def test_the_log_is_owner_only(self):
        discard.record("wallet-04", "frogslime", 61)
        self.assertEqual(discard.LOG_PATH.stat().st_mode & 0o777, 0o600)
