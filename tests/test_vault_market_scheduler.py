import datetime as _dt
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slcw import market as market_mod
from slcw import scheduler, vault as vault_mod
from slcw.config import Config
from slcw.model import parse_player
from slcw.vault import Vault, VaultError, VaultLocked


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "wallets.enc"
        self._patch = patch.object(vault_mod, "VAULT_PATH", self.path)
        self._patch.start()
        # scrypt at production cost makes the suite slow; the algorithm under test
        # is the same at a lower work factor.
        self._n = patch.object(vault_mod, "SCRYPT_N", 2 ** 12)
        self._n.start()

    def tearDown(self):
        self._n.stop()
        self._patch.stop()
        self.tmp.cleanup()

    def test_locked_vault_refuses_to_list_wallets(self):
        with self.assertRaises(VaultLocked):
            Vault().wallets()

    def test_create_encrypt_and_reopen_roundtrip(self):
        vault = Vault()
        vault.unlock("correct horse battery staple")
        created = vault.create_wallets(2)
        self.assertEqual(len(created), 2)

        reopened = Vault()
        count = reopened.unlock("correct horse battery staple")
        self.assertEqual(count, 2)
        self.assertEqual(reopened.get(created[0]["id"])["private_key"],
                         created[0]["private_key"])

    def test_wrong_passphrase_is_rejected(self):
        vault = Vault()
        vault.unlock("right passphrase")
        vault.create_wallets(1)
        with self.assertRaises(VaultError):
            Vault().unlock("wrong passphrase")

    def test_ciphertext_does_not_contain_the_private_key(self):
        vault = Vault()
        vault.unlock("passphrase here")
        created = vault.create_wallets(1)
        raw = self.path.read_text()
        self.assertNotIn(created[0]["private_key"], raw)
        self.assertNotIn(created[0]["public_key"], raw)

    def test_vault_file_is_owner_only(self):
        vault = Vault()
        vault.unlock("passphrase here")
        vault.create_wallets(1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_public_summary_never_leaks_private_keys(self):
        vault = Vault()
        vault.unlock("passphrase here")
        vault.create_wallets(2)
        rendered = json.dumps(vault.public_summary())
        for wallet in vault.wallets():
            self.assertNotIn(wallet["private_key"], rendered)

    def test_wallets_get_distinct_nicknames_and_ids(self):
        vault = Vault()
        vault.unlock("passphrase here")
        created = vault.create_wallets(8)
        self.assertEqual(len({w["id"] for w in created}), 8)
        self.assertEqual(len({w["nickname"] for w in created}), 8)

    def test_each_wallet_gets_its_own_sleep_schedule(self):
        vault = Vault()
        vault.unlock("passphrase here")
        created = vault.create_wallets(10)
        anchors = {w["sleep_anchor_hour"] for w in created}
        self.assertGreater(len(anchors), 1, "wallets must not share one sleep window")

    def test_sleep_hours_falls_within_the_configured_range(self):
        from slcw.vault import SLEEP_HOURS_RANGE
        vault = Vault()
        vault.unlock("passphrase here")
        created = vault.create_wallets(10)
        low, high = SLEEP_HOURS_RANGE
        for wallet in created:
            self.assertGreaterEqual(wallet["sleep_hours"], low)
            self.assertLessEqual(wallet["sleep_hours"], high)

    def test_legacy_plaintext_is_imported_and_removed(self):
        legacy = Path(self.tmp.name) / "wallets.json"
        legacy.write_text(json.dumps([{
            "id": "wallet-01", "nickname": "IronVale82",
            "public_key": "PUB", "private_key": "PRIV"}]))
        with patch.object(vault_mod, "LEGACY_PATH", legacy):
            vault = Vault()
            vault.unlock("passphrase here")
            added = vault.import_legacy("passphrase here")
            self.assertEqual(added, 1)
            self.assertFalse(legacy.exists())
            self.assertEqual(vault.get("wallet-01")["private_key"], "PRIV")


class MarketTests(unittest.TestCase):
    ORDERS = [
        # 565 listed, 35 already filled — only 530 is actually available.
        {"status": "open", "type": "sell", "templateId": "copper_ingot",
         "price": 1500, "quantity": 565, "filled": 35},
        {"status": "open", "type": "sell", "templateId": "copper_ingot",
         "price": 1400, "quantity": 400, "filled": 0},
        {"status": "open", "type": "buy", "templateId": "copper_ingot",
         "price": 1200, "quantity": 100, "filled": 0},
        {"status": "closed", "type": "buy", "templateId": "copper_ingot",
         "price": 9999, "quantity": 100, "filled": 0},
        {"status": "open", "type": "sell", "templateId": "spiderfang",
         "price": 90, "quantity": 10, "filled": 10},
    ]

    def test_best_bid_and_ask(self):
        book = market_mod.build_snapshot(self.ORDERS).books["copper_ingot"]
        self.assertEqual(book.best_ask, 1400)
        self.assertEqual(book.best_bid, 1200)
        self.assertEqual(book.spread, 200)
        self.assertFalse(book.is_crossed)

    def test_closed_orders_are_ignored(self):
        book = market_mod.build_snapshot(self.ORDERS).books["copper_ingot"]
        self.assertNotEqual(book.best_bid, 9999)

    def test_depth_subtracts_filled_quantity(self):
        # The previous monitor summed raw quantity and overstated liquidity.
        book = market_mod.build_snapshot(self.ORDERS).books["copper_ingot"]
        ask_depth = sum(level.quantity for level in book.asks)
        self.assertEqual(ask_depth, 530 + 400)

    def test_fully_filled_order_creates_no_book(self):
        self.assertNotIn("spiderfang", market_mod.build_snapshot(self.ORDERS).books)

    def test_crossed_book_is_detected(self):
        orders = [
            {"status": "open", "type": "buy", "templateId": "x",
             "price": 500, "quantity": 10, "filled": 0},
            {"status": "open", "type": "sell", "templateId": "x",
             "price": 300, "quantity": 10, "filled": 0},
        ]
        snapshot = market_mod.build_snapshot(orders)
        self.assertTrue(snapshot.books["x"].is_crossed)
        self.assertEqual([b.template_id for b in snapshot.crossed()], ["x"])

    def test_holdings_valued_at_best_bid(self):
        snapshot = market_mod.build_snapshot(self.ORDERS)
        self.assertEqual(snapshot.value_of({"copper_ingot": 3}), 3600)

    def test_unpriced_item_contributes_nothing(self):
        snapshot = market_mod.build_snapshot(self.ORDERS)
        self.assertEqual(snapshot.value_of({"unknown_item": 100}), 0)

    def test_freshness_window(self):
        snapshot = market_mod.build_snapshot(self.ORDERS)
        self.assertTrue(snapshot.is_fresh(1800))
        self.assertFalse(market_mod.MarketSnapshot().is_fresh(1800))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.rng = random.Random(11)

    def test_reaction_delay_respects_bounds(self):
        for _ in range(500):
            delay = scheduler.reaction_delay(self.config, self.rng)
            self.assertGreaterEqual(delay, self.config.reaction_min_seconds)
            self.assertLessEqual(delay, self.config.reaction_max_seconds)

    def test_reaction_delays_are_not_uniform(self):
        samples = [scheduler.reaction_delay(self.config, self.rng) for _ in range(400)]
        self.assertGreater(len(set(round(s, 3) for s in samples)), 300)

    def test_wake_times_never_repeat_a_fixed_interval(self):
        # The old timer produced exactly 300s between every run.
        state = parse_player({"attributes": {"vitality": 3, "wisdom": 3},
                              "currentHealth": 130, "currentMana": 130,
                              "activity": None})
        wallet = {"sleep_anchor_hour": 3, "sleep_hours": 7}
        noon = _dt.datetime(2026, 8, 16, 12, 0, tzinfo=_dt.timezone.utc)
        delays = {round(scheduler.next_wake_seconds(
            self.config, wallet, state, now=noon, rng=self.rng)[0], 4)
            for _ in range(200)}
        self.assertGreater(len(delays), 190)

    def test_busy_wallet_wakes_after_the_server_activity_ends(self):
        state = parse_player({
            "attributes": {"vitality": 3, "wisdom": 3},
            "currentHealth": 130, "currentMana": 130,
            "activity": {"type": "production",
                         "endTime": {"seconds": int(_dt.datetime.now(
                             _dt.timezone.utc).timestamp()) + 600}},
        })
        wallet = {"sleep_anchor_hour": 3, "sleep_hours": 7}
        noon = _dt.datetime(2026, 8, 16, 12, 0, tzinfo=_dt.timezone.utc)
        delay, reason = scheduler.next_wake_seconds(
            self.config, wallet, state, now=noon, rng=self.rng)
        self.assertGreater(delay, 600)
        self.assertIn("production", reason)

    def test_sleep_window_defers_the_wallet(self):
        wallet = {"sleep_anchor_hour": 2, "sleep_hours": 7}
        inside = _dt.datetime(2026, 8, 16, 4, 0, tzinfo=_dt.timezone.utc)
        delay, reason = scheduler.next_wake_seconds(
            self.config, wallet, None, now=inside, rng=self.rng)
        self.assertEqual(reason, "sleep window")
        self.assertGreater(delay, 3600 * 4)

    def test_sleep_window_wrapping_midnight(self):
        window = scheduler.SleepWindow(anchor_hour=22, hours=6)
        self.assertTrue(window.contains(_dt.datetime(2026, 8, 16, 23, 0)))
        self.assertTrue(window.contains(_dt.datetime(2026, 8, 16, 2, 0)))
        self.assertFalse(window.contains(_dt.datetime(2026, 8, 16, 12, 0)))

    def test_last_seconds_of_the_window_do_not_cost_a_whole_day(self):
        """wallet-08, 2026-08-28: window 03:00-06:28:12, tick at 06:28:53.

        `contains` dropped the seconds and still said "asleep"; the countdown
        kept them, saw the moment as already past the end, and wrapped the
        remainder round the clock. The wallet slept 24.06 hours and threw away
        a day of play plus its three free energy refills.
        """
        window = scheduler.SleepWindow(anchor_hour=3, hours=3.47)
        edge = _dt.datetime(2026, 8, 28, 6, 28, 53, tzinfo=_dt.timezone.utc)
        self.assertFalse(window.contains(edge))
        self.assertEqual(window.seconds_until_wake(edge), 0.0)

    def test_a_sleeping_wallet_never_waits_longer_than_its_window(self):
        for anchor, hours in ((3, 3.47), (22, 3.7), (21, 3.15), (0, 4.0)):
            window = scheduler.SleepWindow(anchor_hour=anchor, hours=hours)
            for minute in range(0, 24 * 60):
                moment = _dt.datetime(2026, 8, 28, tzinfo=_dt.timezone.utc) \
                    + _dt.timedelta(minutes=minute, seconds=53)
                self.assertLessEqual(window.seconds_until_wake(moment),
                                     hours * 3600.0,
                                     f"anchor={anchor} hours={hours} at {moment:%H:%M:%S}")

    def test_awake_wallet_is_not_deferred(self):
        wallet = {"sleep_anchor_hour": 2, "sleep_hours": 7}
        outside = _dt.datetime(2026, 8, 16, 15, 0, tzinfo=_dt.timezone.utc)
        _, reason = scheduler.next_wake_seconds(
            self.config, wallet, None, now=outside, rng=self.rng)
        self.assertEqual(reason, "idle poll")


if __name__ == "__main__":
    unittest.main()


class ActionReadyPacingTests(unittest.TestCase):
    """A one-minute battle must not be followed by a quarter-hour wait."""

    def setUp(self):
        self.config = Config()
        self.rng = random.Random(3)
        self.wallet = {"sleep_anchor_hour": 3, "sleep_hours": 7}
        self.noon = _dt.datetime(2026, 8, 16, 12, 0, tzinfo=_dt.timezone.utc)
        self.idle = parse_player({"attributes": {"vitality": 3, "wisdom": 3},
                                  "currentHealth": 130, "currentMana": 130,
                                  "activity": None})

    def _wake(self, action_ready):
        return scheduler.next_wake_seconds(
            self.config, self.wallet, self.idle, now=self.noon,
            rng=self.rng, action_ready=action_ready)

    def test_ready_wallet_returns_on_a_reaction_delay(self):
        delay, reason = self._wake(action_ready=True)
        self.assertEqual(reason, "action ready")
        self.assertLessEqual(delay, self.config.reaction_max_seconds)
        self.assertGreaterEqual(delay, self.config.reaction_min_seconds)

    def test_idle_wallet_still_uses_the_slow_poll(self):
        delay, reason = self._wake(action_ready=False)
        self.assertEqual(reason, "idle poll")
        self.assertGreaterEqual(delay, self.config.idle_min_seconds)

    def test_ready_is_typically_much_sooner_than_idle(self):
        ready = [self._wake(True)[0] for _ in range(200)]
        idle = [self._wake(False)[0] for _ in range(200)]
        self.assertLess(sum(ready) / len(ready), sum(idle) / len(idle))

    def test_sleep_window_still_wins_over_a_ready_action(self):
        inside = _dt.datetime(2026, 8, 16, 4, 0, tzinfo=_dt.timezone.utc)
        _, reason = scheduler.next_wake_seconds(
            self.config, self.wallet, self.idle, now=inside,
            rng=self.rng, action_ready=True)
        self.assertEqual(reason, "sleep window")

    def test_running_activity_still_wins_over_a_ready_action(self):
        busy = parse_player({
            "attributes": {"vitality": 3, "wisdom": 3},
            "currentHealth": 130, "currentMana": 130,
            "activity": {"type": "production",
                         "endTime": {"seconds": int(_dt.datetime.now(
                             _dt.timezone.utc).timestamp()) + 600}}})
        _, reason = scheduler.next_wake_seconds(
            self.config, self.wallet, busy, now=self.noon,
            rng=self.rng, action_ready=True)
        self.assertIn("production", reason)

    def test_ready_delays_are_still_varied(self):
        samples = {round(self._wake(True)[0], 3) for _ in range(200)}
        self.assertGreater(len(samples), 190, "pacing must not become a fixed interval")
