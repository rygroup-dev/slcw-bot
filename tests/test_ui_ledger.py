import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import ui
from slcw import ledger as ledger_mod
from slcw.config import Config
from slcw.market import build_snapshot


class KeyboardTests(unittest.TestCase):
    def _callbacks(self, markup: str) -> list[str]:
        rows = json.loads(markup)["inline_keyboard"]
        return [button["callback_data"] for row in rows for button in row]

    def test_main_menu_exposes_every_section(self):
        callbacks = self._callbacks(ui.main_menu())
        for expected in ("nav:status", "nav:profit", "nav:market",
                         "wallet:list", "nav:control", "nav:vault"):
            self.assertIn(expected, callbacks)

    def test_every_callback_uses_a_routed_namespace(self):
        routed = {"nav", "ctl", "wallet", "vault"}
        from slcw.keys import Candidate
        markups = [
            ui.main_menu(),
            ui.economy_menu(),
            ui.control_menu(0, 3, True),
            ui.wallet_list([{"id": "wallet-01", "nickname": "n"}], {}),
            ui.wallet_detail("wallet-01", False),
            ui.new_wallet_menu(),
            ui.import_choice_menu([
                Candidate("Pub1" * 11, "priv", "seed phrase m/44'/501'/0'/0'"),
                Candidate("Pub2" * 11, "priv", "seed phrase, no derivation path"),
            ]),
            ui.vault_menu(True),
            ui.vault_menu(False),
        ]
        for markup in markups:
            for data in self._callbacks(markup):
                self.assertIn(data.split(":")[0], routed, f"unrouted callback {data!r}")

    def test_callback_data_stays_within_telegram_limit(self):
        for markup in (ui.main_menu(), ui.wallet_detail("wallet-99", True)):
            for data in self._callbacks(markup):
                self.assertLessEqual(len(data.encode()), 64)

    def test_wallet_list_offers_creation_and_a_row_per_wallet(self):
        wallets = [{"id": f"wallet-{i:02d}", "nickname": f"n{i}"} for i in range(1, 4)]
        callbacks = self._callbacks(ui.wallet_list(wallets, {}))
        self.assertIn("wallet:new", callbacks)
        for wallet in wallets:
            self.assertIn(f"wallet:show:{wallet['id']}", callbacks)

    def test_detail_toggles_between_pause_and_resume(self):
        self.assertIn("wallet:pause:w1", self._callbacks(ui.wallet_detail("w1", False)))
        self.assertIn("wallet:resume:w1", self._callbacks(ui.wallet_detail("w1", True)))

    def test_dry_run_label_reflects_state(self):
        self.assertIn("Dry-run: ON", ui.control_menu(0, 1, True))
        self.assertIn("Dry-run: OFF", ui.control_menu(0, 1, False))


class RenderTests(unittest.TestCase):
    STATE = {
        "unlocked": True, "dry_run": False, "enabled": True, "market_age_s": 120,
        "wallets": {
            "wallet-01": {
                "wallet_id": "wallet-01", "nickname": "IronVale82", "paused": False,
                "last_action": "startProduction", "last_reason": "1000 gold over 18m",
                "last_run_ts": 0, "next_wake_ts": 0, "next_wake_reason": "production",
                "rationale": ["Chose: startProduction — 1000 gold over 18m"],
                "state": {"level": 6, "gold": 1000, "diamonds": 0, "energy": 79,
                          "max_energy": 100, "health": 124, "max_health": 130,
                          "mana": 130, "max_mana": 130, "location": "city_2",
                          "activity": "production", "activity_remaining_s": 600},
            }
        },
    }

    def test_locked_vault_tells_the_operator_what_to_do(self):
        text = ui.render_status({"unlocked": False})
        self.assertIn("/unlock", text)

    def test_status_shows_wallet_vitals(self):
        text = ui.render_status(self.STATE)
        self.assertIn("wallet-01", text)
        self.assertIn("IronVale82", text)
        self.assertIn("startProduction", text)
        self.assertIn("124/130", text)

    def test_status_escapes_html_in_nicknames(self):
        state = json.loads(json.dumps(self.STATE))
        state["wallets"]["wallet-01"]["nickname"] = "<script>x</script>"
        text = ui.render_status(state)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_status_fits_a_telegram_message(self):
        state = {"unlocked": True, "dry_run": False, "enabled": True, "wallets": {
            f"wallet-{i:02d}": self.STATE["wallets"]["wallet-01"] for i in range(1, 11)}}
        self.assertLess(len(ui.render_status(state)), 4000)

    def test_empty_fleet_guides_to_wallet_creation(self):
        text = ui.render_status({"unlocked": True, "wallets": {}})
        self.assertIn("Belum ada wallet", text)
        self.assertIn("Wallets", text)

    def test_status_paginates_a_large_fleet(self):
        wallets = {f"wallet-{i:02d}": self.STATE["wallets"]["wallet-01"]
                   for i in range(1, 13)}
        state = dict(self.STATE, wallets=wallets)
        pages = ui.page_count(len(wallets))
        self.assertGreater(pages, 1)
        for page in range(1, pages + 1):
            text = ui.render_status(state, page)
            self.assertLess(len(text), 4000, f"page {page} exceeds Telegram's limit")
            self.assertIn(f"Halaman {page}/{pages}", text)

    def test_status_header_summarises_the_whole_fleet(self):
        wallets = {f"wallet-{i:02d}": self.STATE["wallets"]["wallet-01"]
                   for i in range(1, 8)}
        text = ui.render_status(dict(self.STATE, wallets=wallets), 1)
        # Seven wallets holding 1000 gold each, all active.
        self.assertIn("7/7 aktif", text)
        self.assertIn("7,000g total", text)

    def test_page_beyond_the_end_clamps(self):
        text = ui.render_status(self.STATE, page=99)
        self.assertIn("wallet-01", text)

    def test_status_menu_shows_arrows_only_when_needed(self):
        single = json.loads(ui.status_menu(1, 1))["inline_keyboard"]
        many = json.loads(ui.status_menu(2, 4))["inline_keyboard"]
        self.assertNotIn("nav:status:1", [b["callback_data"] for b in single[0]])
        self.assertIn("nav:status:1", [b["callback_data"] for b in many[0]])
        self.assertIn("nav:status:3", [b["callback_data"] for b in many[0]])

    def test_status_menu_wraps_at_both_ends(self):
        first = [b["callback_data"] for b in json.loads(ui.status_menu(1, 3))
                 ["inline_keyboard"][0]]
        last = [b["callback_data"] for b in json.loads(ui.status_menu(3, 3))
                ["inline_keyboard"][0]]
        self.assertIn("nav:status:3", first, "back from page 1 wraps to the last")
        self.assertIn("nav:status:1", last, "forward from the last wraps to page 1")

    def test_paginated_menu_keeps_the_same_home_grid(self):
        plain = {b["callback_data"] for row in
                 json.loads(ui.main_menu())["inline_keyboard"] for b in row}
        paged = {b["callback_data"] for row in
                 json.loads(ui.status_menu(1, 3))["inline_keyboard"] for b in row}
        self.assertTrue(plain - {"nav:status"} <= paged,
                        "the paginated menu must not lose any section")

    def test_market_render_flags_crossed_books(self):
        snapshot = build_snapshot([
            {"status": "open", "type": "buy", "templateId": "x",
             "price": 500, "quantity": 10, "filled": 0},
            {"status": "open", "type": "sell", "templateId": "x",
             "price": 300, "quantity": 10, "filled": 0},
        ])
        text = ui.render_market(snapshot)
        self.assertIn("crossed", text.lower())
        self.assertIn("manual", text.lower())

    def test_wallet_render_never_prints_a_private_key(self):
        wallet = {"id": "wallet-01", "nickname": "n", "public_key": "PUB",
                  "private_key": "SUPERSECRET"}
        text = ui.render_wallet(wallet, self.STATE["wallets"]["wallet-01"])
        self.assertNotIn("SUPERSECRET", text)
        self.assertIn("PUB", text)

    def test_why_view_shows_the_recorded_rationale(self):
        text = ui.render_why(self.STATE["wallets"]["wallet-01"])
        self.assertIn("startProduction", text)

    def test_why_view_handles_no_history(self):
        self.assertIn("Belum ada", ui.render_why({"wallet_id": "w1"}))

    def test_farming_view_prices_every_site(self):
        market = build_snapshot([{"status": "open", "type": "buy",
                                  "templateId": "copper_ore", "price": 600,
                                  "quantity": 999, "filled": 0}])
        text = ui.render_farming(market, level=6, grade=1, gold=5000, energy=80,
                                 config=Config())
        for site in ("farm_1", "farm_2", "farm_4", "farm_5"):
            self.assertIn(site, text)
        self.assertIn("copper_ore", text)
        self.assertIn("tidak diperdagangkan", text)

    def test_farming_view_fits_a_telegram_message(self):
        text = ui.render_farming(None, level=90, grade=7, gold=10**7, energy=100,
                                 config=Config())
        self.assertLess(len(text), 4000)

    def test_combat_view_reports_learned_zones(self):
        from slcw.combat import CombatMemory
        memory = CombatMemory(path=Path(self.tmp.name) / "combat.json") \
            if hasattr(self, "tmp") else CombatMemory(
                path=Path(tempfile.mkdtemp()) / "combat.json")
        for _ in range(10):
            memory.observe("spider", {"player": {"zone": "head", "type": "blocked"},
                                      "monster": {"zone": "torso", "type": "hit"}})
        text = ui.render_combat(memory)
        self.assertIn("spider", text)
        self.assertIn("torso", text)

    def test_chain_view_shows_every_workshop_and_its_margin(self):
        from slcw import refining
        market = build_snapshot([{"status": "open", "type": "buy",
                                  "templateId": "copper_ingot", "price": 888,
                                  "quantity": 999, "filled": 0}])
        text = ui.render_chain(market, Config())
        for workshop in refining.WORKSHOPS:
            self.assertIn(workshop, text)
        self.assertIn("copper_ingot", text)
        # Every cost in the chain must be shown, not just the payoff.
        self.assertIn("copper_ore", text)
        self.assertIn("smelting_flux_1", text)
        self.assertIn("modal", text)
        # 9 ore (~37g) + catalyst (20g) + refine (5g) = 62g against an 888 bid.
        self.assertIn("62g", text)
        self.assertIn("+826g", text)
        self.assertIn("🟢", text)

    def test_chain_view_flags_unpriced_outputs(self):
        text = ui.render_chain(build_snapshot([]), Config())
        self.assertIn("belum ada bid", text)

    def test_chain_view_fits_a_telegram_message(self):
        self.assertLess(len(ui.render_chain(build_snapshot([]), Config())), 4000)

    def test_refining_view_reports_shortfalls(self):
        text = ui.render_refining(build_snapshot([]), level=6, grade=1, gold=100,
                                  holdings={}, config=Config())
        self.assertIn("kurang", text)
        self.assertIn("copper_ore", text)

    def test_refining_view_shows_a_feasible_run(self):
        market = build_snapshot([{"status": "open", "type": "buy",
                                  "templateId": "copper_ingot", "price": 888,
                                  "quantity": 999, "filled": 0}])
        text = ui.render_refining(
            market, level=6, grade=1, gold=5000,
            holdings={"copper_ore": 90, "smelting_flux_1": 10}, config=Config())
        self.assertIn("✅", text)
        self.assertIn("copper_ingot", text)

    def test_energy_view_shows_remaining_refills(self):
        state = {"wallets": {"wallet-01": {
            "nickname": "n", "state": {"energy": 20, "max_energy": 100,
                                       "free_refills_left": 2}}}}
        text = ui.render_energy(state)
        self.assertIn("2/3", text)
        self.assertIn("🟢🟢⚪", text)

    def test_energy_view_handles_missing_data(self):
        self.assertIn("Belum ada", ui.render_energy({"wallets": {}}))

    def test_combat_view_handles_empty_memory(self):
        from slcw.combat import CombatMemory
        memory = CombatMemory(path=Path(tempfile.mkdtemp()) / "combat.json")
        self.assertIn("Belum ada data", ui.render_combat(memory))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        self._patch = patch.object(ledger_mod, "LEDGER_PATH", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_records_production_reward(self):
        row = ledger_mod.record("wallet-01", "finishActivity", {
            "rewardSummary": {"type": "farming", "gold": 1000}})
        self.assertEqual(row["reward"]["gold"], 1000)
        self.assertEqual(ledger_mod.totals().gold, 1000)

    def test_records_nested_battle_reward(self):
        ledger_mod.record("wallet-01", "battle", {"reward": {"rewardSummary": {
            "type": "battle", "winner": "player", "xp": 22, "gold": 0,
            "items": [{"id": "spiderfang", "quantity": 2}]}}})
        totals = ledger_mod.totals()
        self.assertEqual(totals.xp, 22)
        self.assertEqual(totals.items["spiderfang"], 2)
        self.assertEqual(totals.battles_won, 1)

    def test_records_nested_task_battle_reward(self):
        # startTaskBattle returns the same run_battle-shaped result as a
        # normal battle — reward nested under "reward", not top-level.
        ledger_mod.record("wallet-01", "startTaskBattle", {"reward": {"rewardSummary": {
            "type": "battle", "winner": "player", "xp": 30, "gold": 0,
            "items": [{"id": "shadowclaw", "quantity": 1}]}}})
        totals = ledger_mod.totals()
        self.assertEqual(totals.xp, 30)
        self.assertEqual(totals.items["shadowclaw"], 1)
        self.assertEqual(totals.battles_won, 1)

    def test_records_newbie_quest_xp(self):
        # completeNewbieQuest's own shape: {"success": true, "xpGained": N,
        # "nextQuest": M} — no "rewardSummary" key at all.
        ledger_mod.record("wallet-01", "completeNewbieQuest",
                          {"success": True, "xpGained": 400, "nextQuest": 6})
        self.assertEqual(ledger_mod.totals().xp, 400)

    def test_records_hunt_task_gold(self):
        # claimTaskReward's own shape: {goldAwarded, allTasksCompleted}.
        ledger_mod.record("wallet-01", "claimTaskReward",
                          {"goldAwarded": 5000, "allTasksCompleted": False})
        self.assertEqual(ledger_mod.totals().gold, 5000)

    def test_rewardless_actions_write_nothing(self):
        self.assertIsNone(ledger_mod.record("wallet-01", "startRelax", {"success": True}))
        self.assertFalse(self.path.exists())

    def test_totals_filter_by_wallet(self):
        ledger_mod.record("wallet-01", "finishActivity",
                          {"rewardSummary": {"gold": 1000}})
        ledger_mod.record("wallet-02", "finishActivity",
                          {"rewardSummary": {"gold": 500}})
        self.assertEqual(ledger_mod.totals("wallet-01").gold, 1000)
        self.assertEqual(ledger_mod.totals().gold, 1500)

    def test_corrupt_lines_are_skipped(self):
        ledger_mod.record("wallet-01", "finishActivity", {"rewardSummary": {"gold": 10}})
        with self.path.open("a") as handle:
            handle.write("{ not json\n")
        self.assertEqual(ledger_mod.totals().gold, 10)

    def test_win_rate_counts_losses(self):
        ledger_mod.record("w", "battle", {"reward": {"rewardSummary": {
            "type": "battle", "winner": "player", "xp": 10}}})
        ledger_mod.record("w", "battle", {"reward": {"rewardSummary": {
            "type": "battle", "winner": "monster", "xp": 2}}})
        self.assertEqual(ledger_mod.totals().win_rate, 0.5)

    def test_item_value_uses_market_bids(self):
        ledger_mod.record("w", "battle", {"reward": {"rewardSummary": {
            "type": "battle", "winner": "player",
            "items": [{"id": "spiderfang", "quantity": 4}]}}})
        snapshot = build_snapshot([{"status": "open", "type": "buy",
                                    "templateId": "spiderfang", "price": 25,
                                    "quantity": 100, "filled": 0}])
        _, value = ledger_mod.valued_totals(market=snapshot)
        self.assertEqual(value, 100)


class ConfigTests(unittest.TestCase):
    def test_validate_reports_missing_credentials(self):
        problems = Config(firebase_api_key="", telegram_token="",
                          telegram_chat_id="").validate()
        self.assertEqual(len(problems), 3)

    def test_complete_config_has_no_problems(self):
        self.assertEqual(Config(firebase_api_key="k", telegram_token="t",
                                telegram_chat_id="1").validate(), [])

    def test_inverted_jitter_bounds_are_caught(self):
        problems = Config(firebase_api_key="k", telegram_token="t", telegram_chat_id="1",
                          idle_min_seconds=900, idle_max_seconds=100).validate()
        self.assertTrue(any("IDLE" in p for p in problems))


if __name__ == "__main__":
    unittest.main()


class MapAndTaskViewTests(unittest.TestCase):
    def test_map_view_lists_distances_from_each_wallet(self):
        state = {"wallets": {"wallet-01": {
            "nickname": "n", "state": {"location": "farm_2"}}}}
        text = ui.render_map(state)
        self.assertIn("Crystal Cave", text)
        # city_1 hosts the smelter that consumes what farm_2 gathers.
        self.assertIn("Agnos", text)
        self.assertIn("smelting", text)

    def test_map_view_handles_unknown_position(self):
        self.assertIn("Belum ada", ui.render_map({"wallets": {}}))

    def test_map_view_fits_a_telegram_message(self):
        state = {"wallets": {f"wallet-{i:02d}": {
            "nickname": "n", "state": {"location": "farm_2"}} for i in range(1, 9)}}
        self.assertLess(len(ui.render_map(state)), 4000)

    def test_task_view_explains_the_level_gate(self):
        from slcw import tasks
        status = tasks.parse_status({"playerLevel": 4})
        self.assertIn("level 10", ui.render_tasks(status))

    def test_task_view_shows_progress(self):
        from slcw import tasks
        status = tasks.parse_status({
            "playerLevel": 12, "completedCount": 2, "hasActiveTask": True,
            "task": {"taskIndex": 1, "monsterId": "forestspider_lvl1_2",
                     "monsterLevel": 1, "killsRequired": 10, "killsProgress": 7,
                     "status": "active", "goldReward": 5000}})
        text = ui.render_tasks(status)
        self.assertIn("7/10", text)
        self.assertIn("5,000 gold", text)

    def test_task_view_without_data(self):
        self.assertIn("Belum ada data", ui.render_tasks(None))
