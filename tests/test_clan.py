import datetime as _dt
import unittest

from slcw import clan


def _ms(dt):
    return int(dt.timestamp() * 1000)


NOW = _dt.datetime(2026, 8, 21, 12, 0, tzinfo=_dt.timezone.utc)


class DonationRateTests(unittest.TestCase):
    """Rates read from the client: 1,000 Gold = 1 DKP, 1 $SLCW = 50 DKP."""

    def test_gold_converts_at_a_thousand_to_one(self):
        self.assertEqual(clan.donation_dkp(5_000, "gold"), 5)

    def test_gold_below_the_rate_buys_nothing(self):
        self.assertEqual(clan.donation_dkp(999, "gold"), 0)

    def test_slcw_converts_at_one_to_fifty(self):
        self.assertEqual(clan.donation_dkp(2, "slcw"), 100)

    def test_minimums_match_the_client(self):
        self.assertEqual(clan.minimum_donation("gold"), 1_000)
        self.assertEqual(clan.minimum_donation("slcw"), 1)


class AffordableDonationTests(unittest.TestCase):
    def test_amount_is_rounded_down_to_whole_dkp(self):
        # 4,500 gold buys 4 DKP; the trailing 500 would be given away for nothing.
        self.assertEqual(clan.affordable_donation(4_500), 4_000)

    def test_the_reserve_is_never_spent(self):
        self.assertEqual(clan.affordable_donation(5_000, reserve=2_000), 3_000)

    def test_nothing_is_offered_below_the_minimum(self):
        self.assertEqual(clan.affordable_donation(900), 0)

    def test_a_reserve_larger_than_the_balance_yields_nothing(self):
        self.assertEqual(clan.affordable_donation(1_000, reserve=5_000), 0)


class MembershipTests(unittest.TestCase):
    def test_a_wallet_with_no_clan_is_not_a_member(self):
        self.assertFalse(clan.ClanMembership().is_member)

    def test_a_recruit_is_on_probation_for_three_days(self):
        m = clan.ClanMembership(clan_id="c", role="initiate",
                                joined_at_ms=_ms(NOW - _dt.timedelta(days=1)))
        self.assertTrue(m.on_probation(NOW))
        self.assertFalse(m.can_donate(NOW))

    def test_probation_ends_after_three_days(self):
        m = clan.ClanMembership(clan_id="c", role="initiate",
                                joined_at_ms=_ms(NOW - _dt.timedelta(days=4)))
        self.assertFalse(m.on_probation(NOW))

    def test_a_full_member_is_never_on_probation(self):
        m = clan.ClanMembership(clan_id="c", role="member",
                                joined_at_ms=_ms(NOW - _dt.timedelta(hours=1)))
        self.assertFalse(m.on_probation(NOW))

    def test_donating_earlier_today_blocks_another_donation(self):
        m = clan.ClanMembership(clan_id="c", role="member",
                                last_donation_ms=_ms(NOW - _dt.timedelta(hours=3)))
        self.assertTrue(m.donated_today(NOW))
        self.assertFalse(m.can_donate(NOW))

    def test_the_window_resets_at_midnight_utc_not_after_24h(self):
        """23:50 yesterday and 00:10 today are 20 minutes apart but different days."""
        just_before_midnight = _dt.datetime(2026, 8, 20, 23, 50, tzinfo=_dt.timezone.utc)
        just_after = _dt.datetime(2026, 8, 21, 0, 10, tzinfo=_dt.timezone.utc)
        m = clan.ClanMembership(clan_id="c", role="member",
                                last_donation_ms=_ms(just_before_midnight))
        self.assertFalse(m.donated_today(just_after))
        self.assertTrue(m.can_donate(just_after))

    def test_a_member_who_has_never_donated_may_donate(self):
        m = clan.ClanMembership(clan_id="c", role="member")
        self.assertTrue(m.can_donate(NOW))


class QuestTests(unittest.TestCase):
    def _quest(self, **over):
        doc = {
            "requirements": [
                {"itemId": "glowingspore", "required": 16_000, "collected": 8_460},
                {"itemId": "frostfang", "required": 16_000, "collected": 11_690},
            ],
            "rewardDkpPool": 8_000,
            "rewardClanXp": 28_000,
            "completedAt": None,
        }
        doc.update(over)
        return clan.parse_quest(doc, quest_id="q1")

    def test_outstanding_counts_what_is_still_missing(self):
        self.assertEqual(self._quest().outstanding(),
                         {"glowingspore": 7_540, "frostfang": 4_310})

    def test_a_satisfied_requirement_drops_out(self):
        quest = self._quest(requirements=[
            {"itemId": "frostfang", "required": 100, "collected": 100}])
        self.assertEqual(quest.outstanding(), {})

    def test_the_largest_useful_stack_is_chosen(self):
        holdings = {"glowingspore": 12, "frostfang": 40, "spiderfang": 900}
        self.assertEqual(clan.submittable(self._quest(), holdings), ("frostfang", 40))

    def test_submission_is_capped_by_what_the_quest_still_needs(self):
        quest = self._quest(requirements=[
            {"itemId": "frostfang", "required": 100, "collected": 95}])
        self.assertEqual(clan.submittable(quest, {"frostfang": 400}), ("frostfang", 5))

    def test_items_the_quest_does_not_want_are_ignored(self):
        self.assertEqual(clan.submittable(self._quest(), {"spiderfang": 900}), ("", 0))

    def test_a_completed_quest_takes_nothing(self):
        quest = self._quest(completedAt="2026-08-15T14:50:41.063Z")
        self.assertTrue(quest.completed)
        self.assertEqual(clan.submittable(quest, {"frostfang": 40}), ("", 0))

    def test_empty_holdings_submit_nothing(self):
        self.assertEqual(clan.submittable(self._quest(), {}), ("", 0))


class ParseTests(unittest.TestCase):
    def test_membership_parses_a_live_member_document(self):
        m = clan.parse_membership("c1", {
            "userId": "u", "role": "member", "dkp": 767,
            "joinedAt": "2026-08-08T15:01:46.680Z",
            "lastDonationAt": "2026-08-18T10:02:03.758Z",
        })
        self.assertEqual((m.clan_id, m.role, m.dkp), ("c1", "member", 767))
        self.assertTrue(m.joined_at_ms > 0 and m.last_donation_ms > 0)

    def test_a_member_who_never_donated_parses_cleanly(self):
        m = clan.parse_membership("c1", {"role": "member", "dkp": 0,
                                         "lastDonationAt": None})
        self.assertEqual(m.last_donation_ms, 0)
        self.assertTrue(m.can_donate(NOW))

    def test_parsing_an_empty_quest_yields_nothing(self):
        self.assertIsNone(clan.parse_quest(None))
        self.assertIsNone(clan.parse_quest({}))


class OrchestratorClanTests(unittest.TestCase):
    """Clan actions have to reach the decision loop, and stay out of it when unsafe."""

    from slcw.config import Config as _Config

    def _state(self, gold=50_000):
        from slcw.model import parse_player
        return parse_player({
            # Above the refill threshold: a free energy refill is worth more
            # than any clan action and would otherwise win every one of these.
            "level": 12, "energy": 80, "maxEnergy": 100, "balance": gold,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 13)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
        })

    def _clan(self, **over):
        ctx = {
            "membership": clan.ClanMembership(
                clan_id="c1", role="member", dkp=10),
            "quest": clan.parse_quest({
                "requirements": [{"itemId": "frostfang", "required": 16_000,
                                  "collected": 100}],
                "rewardDkpPool": 8_000, "rewardClanXp": 28_000,
                "completedAt": None}, quest_id="q1"),
        }
        ctx.update(over)
        return ctx

    def _actions(self, config=None, holdings=None, clan_ctx=None, gold=50_000):
        from tests.test_orchestrator import make, FakeApi
        orch = make(config=config or self._Config(enabled=True, dry_run=False),
                    api=FakeApi())
        return [c.action for c in orch.build_candidates(
            self._state(gold), holdings=holdings or {"frostfang": 500},
            include_travel=False, wallet_id="w1",
            clan_context=clan_ctx if clan_ctx is not None else self._clan())]

    def test_quest_resources_are_submitted_when_the_quest_wants_them(self):
        self.assertIn("submitQuestResources", self._actions())

    def test_nothing_is_submitted_when_the_wallet_holds_none_of_it(self):
        self.assertNotIn("submitQuestResources",
                         self._actions(holdings={"spiderfang": 900}))

    def test_a_wallet_in_no_clan_gets_no_clan_actions(self):
        actions = self._actions(clan_ctx={"membership": clan.ClanMembership(),
                                          "quest": None})
        self.assertNotIn("submitQuestResources", actions)
        self.assertNotIn("makeDonation", actions)

    def test_gold_donation_is_off_by_default(self):
        """It moves gold into a treasury the operator may not control."""
        self.assertNotIn("makeDonation", self._actions())

    def test_quest_resources_outrank_a_gold_donation(self):
        """Spending unsellable drops beats spending gold, so it wins."""
        cfg = self._Config(enabled=True, dry_run=False, clan_donate_gold=True)
        self.assertEqual(self._actions(config=cfg)[0], "submitQuestResources")

    def test_gold_donation_is_offered_once_enabled_and_no_quest_needs_items(self):
        cfg = self._Config(enabled=True, dry_run=False, clan_donate_gold=True)
        ctx = self._clan(quest=None)
        self.assertIn("makeDonation", self._actions(config=cfg, clan_ctx=ctx))

    def test_donation_is_not_offered_twice_in_one_day(self):
        cfg = self._Config(enabled=True, dry_run=False, clan_donate_gold=True)
        import datetime as dt
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        ctx = self._clan(membership=clan.ClanMembership(
            clan_id="c1", role="member", last_donation_ms=now_ms))
        self.assertNotIn("makeDonation", self._actions(config=cfg, clan_ctx=ctx))

    def test_a_recruit_on_probation_does_not_donate(self):
        cfg = self._Config(enabled=True, dry_run=False, clan_donate_gold=True)
        import datetime as dt
        joined = int((dt.datetime.now(dt.timezone.utc)
                      - dt.timedelta(days=1)).timestamp() * 1000)
        ctx = self._clan(membership=clan.ClanMembership(
            clan_id="c1", role="initiate", joined_at_ms=joined))
        self.assertNotIn("makeDonation", self._actions(config=cfg, clan_ctx=ctx))

    def test_donation_respects_the_clan_gold_reserve(self):
        cfg = self._Config(enabled=True, dry_run=False, clan_donate_gold=True,
                           clan_gold_reserve=50_000)
        self.assertNotIn("makeDonation", self._actions(config=cfg, gold=50_000))

    def test_clan_actions_stop_when_the_feature_is_switched_off(self):
        cfg = self._Config(enabled=True, dry_run=False, clan_enabled=False,
                           clan_donate_gold=True)
        actions = self._actions(config=cfg)
        self.assertNotIn("submitQuestResources", actions)
        self.assertNotIn("makeDonation", actions)


class DecideAndActClanTests(unittest.TestCase):
    """clan_context must survive the trip from the runner into the decision."""

    def test_a_clan_submission_is_executed_end_to_end(self):
        from tests.test_orchestrator import make, FakeApi
        from slcw.model import parse_player
        api = FakeApi()
        calls = []
        api.submit_quest_resources = (
            lambda s, c, q, i, a: calls.append((c, q, i, a)) or {"success": True})
        state = parse_player({
            "level": 12, "energy": 80, "maxEnergy": 100, "balance": 50_000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 13)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
        })
        ctx = {
            "membership": clan.ClanMembership(clan_id="c1", role="member"),
            "quest": clan.parse_quest({
                "requirements": [{"itemId": "frostfang", "required": 999,
                                  "collected": 0}],
                "rewardDkpPool": 8_000, "rewardClanXp": 1, "completedAt": None},
                quest_id="q1"),
        }
        decision = make(api=api).decide_and_act(
            {"id": "w1"}, None, state, None, {"frostfang": 40}, None, None,
            clan_context=ctx)
        self.assertEqual(decision.action, "submitQuestResources")
        self.assertEqual(calls, [("c1", "q1", "frostfang", 40)])


if __name__ == "__main__":
    unittest.main()


class FounderReserveTests(unittest.TestCase):
    """The nominated founder must be able to reach 20,000 gold.

    No player-to-player gold transfer exists in this game — twenty candidate
    endpoint names all return 404 — so a clan is funded by one wallet saving up,
    not by the fleet pooling. A wallet that keeps spending its gold on refining
    and gathering never arrives, so while it is nominated it spends none.
    """

    from slcw.config import Config as _Config

    def _reserve(self, wallet_id, gold, founder="wallet-01"):
        from tests.test_orchestrator import make, FakeApi
        from slcw.model import parse_player
        cfg = self._Config(enabled=True, dry_run=False, gold_reserve=500,
                           clan_founder_wallet=founder)
        state = parse_player({"level": 12, "balance": gold, "energy": 50,
                              "maxEnergy": 100, "currentHealth": 130,
                              "currentMana": 130, "attributes": {"vitality": 3}})
        return make(config=cfg, api=FakeApi()).spendable_gold(state, wallet_id)

    def test_the_founder_spends_nothing_while_saving(self):
        self.assertEqual(self._reserve("wallet-01", 12_214), 0)

    def test_other_wallets_are_unaffected(self):
        self.assertEqual(self._reserve("wallet-07", 12_214), 12_214 - 500)

    def test_the_founder_spends_normally_once_it_can_afford_the_clan(self):
        self.assertEqual(self._reserve("wallet-01", 25_000), 25_000 - 500)

    def test_nothing_changes_when_no_founder_is_nominated(self):
        self.assertEqual(self._reserve("wallet-01", 12_214, founder=""),
                         12_214 - 500)


class RegistryTests(unittest.TestCase):
    """The clan is founded exactly once, and that has to survive a restart."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "clan.json"

    def reg(self):
        return clan.ClanRegistry(path=self.path)

    def test_a_fresh_registry_has_no_clan(self):
        self.assertFalse(self.reg().founded)

    def test_a_recorded_clan_survives_a_restart(self):
        self.reg().record_clan("c1", "wallet-01")
        self.assertEqual(self.reg().clan_id, "c1")
        self.assertTrue(self.reg().founded)

    def test_the_first_clan_recorded_is_never_replaced(self):
        r = self.reg()
        r.record_clan("c1", "wallet-01")
        r.record_clan("c2", "wallet-02")
        self.assertEqual(r.clan_id, "c1")

    def test_an_empty_clan_id_is_ignored(self):
        r = self.reg()
        r.record_clan("", "wallet-01")
        self.assertFalse(r.founded)

    def test_a_sent_application_is_remembered(self):
        r = self.reg()
        r.record_application("wallet-02", "app1")
        self.assertTrue(r.has_pending_application("wallet-02"))
        self.assertFalse(r.has_pending_application("wallet-03"))

    def test_an_application_goes_stale_so_a_wallet_can_retry(self):
        import time as _t
        r = self.reg()
        r.record_application("wallet-02", "app1")
        later = _t.time() + clan.APPLICATION_TTL_S + 1
        self.assertFalse(r.has_pending_application("wallet-02", now=later))

    def test_a_corrupt_registry_is_ignored_rather_than_fatal(self):
        self.path.write_text("{not json")
        self.assertFalse(self.reg().founded)


class AcceptableApplicationTests(unittest.TestCase):
    """A leader admits its own fleet and nobody else."""

    OURS = {"solana:AAA", "solana:BBB"}

    def _app(self, **over):
        app = {"clanId": "c1", "userId": "solana:AAA", "status": "pending",
               "resolvedAt": None}
        app.update(over)
        return app

    def test_our_own_pending_application_is_accepted(self):
        got = clan.acceptable_applications([self._app()], "c1", self.OURS)
        self.assertEqual(len(got), 1)

    def test_a_strangers_application_is_never_accepted(self):
        got = clan.acceptable_applications(
            [self._app(userId="solana:ZZZ")], "c1", self.OURS)
        self.assertEqual(got, [])

    def test_an_application_to_another_clan_is_ignored(self):
        got = clan.acceptable_applications([self._app(clanId="other")], "c1", self.OURS)
        self.assertEqual(got, [])

    def test_an_already_resolved_application_is_ignored(self):
        got = clan.acceptable_applications(
            [self._app(resolvedAt="2026-08-20T00:00:00Z")], "c1", self.OURS)
        self.assertEqual(got, [])

    def test_a_rejected_application_is_ignored(self):
        got = clan.acceptable_applications(
            [self._app(status="rejected")], "c1", self.OURS)
        self.assertEqual(got, [])


class AutoFoundJoinTests(unittest.TestCase):
    """Found once from the primary wallet; every other wallet joins by itself."""

    from slcw.config import Config as _Config

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.registry = clan.ClanRegistry(path=Path(self.dir.name) / "clan.json")

    def _cfg(self, **over):
        base = dict(enabled=True, dry_run=False, clan_founder_wallet="wallet-01",
                    clan_auto_found=True, clan_name="Ry HooD", clan_tag="RYH",
                    clan_auto_join=True)
        base.update(over)
        return self._Config(**base)

    def _state(self, gold=25_000, clan_id=""):
        from slcw.model import parse_player
        doc = {"level": 12, "energy": 80, "maxEnergy": 100, "balance": gold,
               "currentHealth": 130, "currentMana": 130,
               "currentLocationId": "farm_3", "attributes": {"wisdom": 3, "vitality": 3},
               "claimedInitialRewardsV2": list(range(1, 13)),
               "newbieQuest": 999, "activity": None,
               "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01"}
        if clan_id:
            doc["clanId"] = clan_id
        return parse_player(doc)

    def _actions(self, wallet_id, state=None, cfg=None, ctx=None):
        from tests.test_orchestrator import make, FakeApi
        context = {"membership": None, "quest": None,
                   "registry": self.registry, "fleet_uids": set(),
                   "applications": []}
        context.update(ctx or {})
        orch = make(config=cfg or self._cfg(), api=FakeApi())
        return [c.action for c in orch.build_candidates(
            state if state is not None else self._state(), include_travel=False,
            wallet_id=wallet_id, clan_context=context)]

    # --- founding ---
    def test_the_primary_wallet_founds_the_clan_when_it_can_afford_it(self):
        self.assertIn("createClan", self._actions("wallet-01"))

    def test_it_does_not_found_while_still_short_of_the_cost(self):
        self.assertNotIn("createClan",
                         self._actions("wallet-01", self._state(gold=19_999)))

    def test_no_other_wallet_ever_founds_a_clan(self):
        self.assertNotIn("createClan", self._actions("wallet-07"))

    def test_it_never_founds_a_second_clan(self):
        """The registry is the guard that survives a restart."""
        self.registry.record_clan("c1", "wallet-01")
        self.assertNotIn("createClan", self._actions("wallet-01"))

    def test_it_does_not_found_when_the_wallet_is_already_in_a_clan(self):
        self.assertNotIn("createClan",
                         self._actions("wallet-01", self._state(clan_id="c9")))

    def test_founding_needs_a_name_and_tag(self):
        cfg = self._cfg(clan_name="", clan_tag="")
        self.assertNotIn("createClan", self._actions("wallet-01", cfg=cfg))

    def test_founding_can_be_switched_off(self):
        self.assertNotIn("createClan",
                         self._actions("wallet-01", cfg=self._cfg(clan_auto_found=False)))

    # --- joining ---
    def test_other_wallets_apply_once_the_clan_exists(self):
        self.registry.record_clan("c1", "wallet-01")
        self.assertIn("applyClan", self._actions("wallet-07"))

    def test_a_wallet_does_not_apply_before_the_clan_exists(self):
        self.assertNotIn("applyClan", self._actions("wallet-07"))

    def test_a_wallet_does_not_reapply_while_its_request_is_pending(self):
        self.registry.record_clan("c1", "wallet-01")
        self.registry.record_application("wallet-07", "app1")
        self.assertNotIn("applyClan", self._actions("wallet-07"))

    def test_a_wallet_already_in_the_clan_does_not_apply(self):
        self.registry.record_clan("c1", "wallet-01")
        self.assertNotIn("applyClan",
                         self._actions("wallet-07", self._state(clan_id="c1")))

    def test_a_wallet_added_later_joins_with_no_extra_step(self):
        """A brand-new wallet is just another wallet with no clan."""
        self.registry.record_clan("c1", "wallet-01")
        self.assertIn("applyClan", self._actions("wallet-99"))

    def test_the_founder_does_not_apply_to_its_own_clan(self):
        """Between createClan returning and clanId appearing on the document."""
        self.registry.record_clan("c1", "wallet-01")
        self.assertNotIn("applyClan", self._actions("wallet-01"))

    def test_joining_can_be_switched_off(self):
        self.registry.record_clan("c1", "wallet-01")
        self.assertNotIn("applyClan",
                         self._actions("wallet-07", cfg=self._cfg(clan_auto_join=False)))

    # --- admitting ---
    def test_the_leader_admits_its_own_fleet(self):
        self.registry.record_clan("c1", "wallet-01")
        ctx = {"membership": clan.ClanMembership(clan_id="c1", role="leader"),
               "fleet_uids": {"solana:AAA"},
               "applications": [{"clanId": "c1", "userId": "solana:AAA",
                                 "status": "pending", "resolvedAt": None,
                                 "applicationId": "app1"}]}
        self.assertIn("resolveApplication",
                      self._actions("wallet-01", self._state(clan_id="c1"), ctx=ctx))

    def test_the_leader_never_admits_a_stranger(self):
        self.registry.record_clan("c1", "wallet-01")
        ctx = {"membership": clan.ClanMembership(clan_id="c1", role="leader"),
               "fleet_uids": {"solana:AAA"},
               "applications": [{"clanId": "c1", "userId": "solana:STRANGER",
                                 "status": "pending", "resolvedAt": None,
                                 "applicationId": "app1"}]}
        self.assertNotIn("resolveApplication",
                         self._actions("wallet-01", self._state(clan_id="c1"), ctx=ctx))

    def test_a_plain_member_does_not_try_to_admit_anyone(self):
        self.registry.record_clan("c1", "wallet-01")
        ctx = {"membership": clan.ClanMembership(clan_id="c1", role="member"),
               "fleet_uids": {"solana:AAA"},
               "applications": [{"clanId": "c1", "userId": "solana:AAA",
                                 "status": "pending", "resolvedAt": None,
                                 "applicationId": "app1"}]}
        self.assertNotIn("resolveApplication",
                         self._actions("wallet-02", self._state(clan_id="c1"), ctx=ctx))


class CapacityTests(unittest.TestCase):
    """Seats and the level curve, measured against every clan live in the game."""

    # Read from Firestore on 2026-08-21: every clan that exists, with the
    # maxMembers and xpRequired the server itself stores.
    LIVE = [
        # (level, maxMembers, xpRequired, name)
        (1, 10, 300, "Wolf"),
        (1, 10, 300, "Team Krm"),
        (12, 65, 4758, "Asgard"),
        (15, 80, 7800, "Dragon Hunters Inc"),
        (15, 80, 7800, "The Shadows"),
        (22, 115, 20148, "Avalon"),
        (27, 140, 34818, "Th0rity"),
        (31, 160, 50928, "LEGION"),
    ]

    def test_max_members_matches_every_live_clan(self):
        for level, seats, _xp, name in self.LIVE:
            self.assertEqual(clan.max_members(level), seats, name)

    def test_a_new_clan_has_ten_seats(self):
        self.assertEqual(clan.max_members(1), 10)

    def test_xp_required_matches_every_live_clan(self):
        for level, _seats, xp, name in self.LIVE:
            self.assertEqual(clan.level_xp_required(level), xp, name)

    def test_levels_for_seats_finds_the_cheapest_level_that_fits(self):
        self.assertEqual(clan.levels_for_seats(10), 1)
        self.assertEqual(clan.levels_for_seats(11), 2)
        self.assertEqual(clan.levels_for_seats(30), 5)
        self.assertEqual(clan.levels_for_seats(35), 6)

    def test_xp_to_reach_sums_the_levels_in_between(self):
        self.assertEqual(clan.xp_to_reach(1, 2), 300)
        self.assertEqual(clan.xp_to_reach(1, 6), 3_417)
        self.assertEqual(clan.xp_to_reach(3, 3), 0)

    def test_one_quest_carries_a_new_clan_to_thirty_five_seats(self):
        """The reason the fleet can outgrow ten seats at all."""
        self.assertGreaterEqual(clan.QUEST_CLAN_XP, clan.xp_to_reach(1, 6))
        self.assertEqual(clan.max_members(6), 35)


class ClanInfoTests(unittest.TestCase):

    def _doc(self, **over):
        doc = {"level": 1, "xp": 100, "xpRequired": 300,
               "maxMembers": 10, "memberCount": 7, "name": "Wolf"}
        doc.update(over)
        return doc

    def test_a_live_clan_document_parses(self):
        info = clan.parse_clan("c1", self._doc())
        self.assertEqual((info.clan_id, info.level, info.max_members,
                          info.member_count), ("c1", 1, 10, 7))

    def test_free_seats_is_what_is_left(self):
        self.assertEqual(clan.parse_clan("c1", self._doc()).free_seats, 3)

    def test_a_full_clan_has_no_free_seats(self):
        info = clan.parse_clan("c1", self._doc(memberCount=10))
        self.assertEqual(info.free_seats, 0)

    def test_an_overfull_clan_never_reports_negative_seats(self):
        info = clan.parse_clan("c1", self._doc(memberCount=12))
        self.assertEqual(info.free_seats, 0)

    def test_max_members_is_derived_when_the_document_omits_it(self):
        doc = self._doc(level=6)
        doc.pop("maxMembers")
        self.assertEqual(clan.parse_clan("c1", doc).max_members, 35)

    def test_an_empty_document_parses_to_nothing(self):
        self.assertIsNone(clan.parse_clan("c1", None))
        self.assertIsNone(clan.parse_clan("c1", {}))


class SeatRankingTests(unittest.TestCase):
    """Ten seats and thirty wallets: the highest levels take them."""

    LEVELS = {"w-a": 5, "w-b": 15, "w-c": 12, "w-d": 15, "w-e": 1}

    def test_the_highest_levels_take_the_seats(self):
        self.assertEqual(clan.seat_ranking(self.LEVELS, 2), ["w-b", "w-d"])

    def test_ties_break_on_wallet_id_so_the_choice_is_stable(self):
        self.assertEqual(clan.seat_ranking(self.LEVELS, 3),
                         ["w-b", "w-d", "w-c"])
        self.assertEqual(clan.seat_ranking(self.LEVELS, 3),
                         clan.seat_ranking(self.LEVELS, 3))

    def test_more_seats_than_wallets_takes_everyone(self):
        self.assertEqual(len(clan.seat_ranking(self.LEVELS, 99)), 5)

    def test_no_seats_admits_nobody(self):
        self.assertEqual(clan.seat_ranking(self.LEVELS, 0), [])
        self.assertEqual(clan.seat_ranking(self.LEVELS, -3), [])

    def test_an_empty_fleet_ranks_to_nothing(self):
        self.assertEqual(clan.seat_ranking({}, 5), [])


class SeatAwareJoinTests(unittest.TestCase):
    """Thirty wallets cannot all apply to a clan with ten seats."""

    from slcw.config import Config as _Config

    def _cfg(self, **over):
        base = dict(enabled=True, dry_run=False, clan_auto_join=True)
        base.update(over)
        return self._Config(**base)

    def _actions(self, wallet_id, clan_ctx, config=None):
        from tests.test_orchestrator import make, FakeApi
        from slcw.model import parse_player
        orch = make(config=config or self._cfg(), api=FakeApi())
        state = parse_player({
            "level": 12, "energy": 80, "maxEnergy": 100, "balance": 5_000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 13)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
        })
        return [c.action for c in orch.build_candidates(
            state, holdings={}, include_travel=False, wallet_id=wallet_id,
            clan_context=clan_ctx)]

    def _registry(self, tmp):
        reg = clan.ClanRegistry(path=tmp)
        reg.record_clan("c1", "wallet-01")
        return reg

    def _ctx(self, reg, holders):
        return {"membership": None, "quest": None, "registry": reg,
                "fleet_uids": set(), "applications": [],
                "clan_info": clan.parse_clan("c1", {
                    "level": 1, "maxMembers": 10, "memberCount": 1}),
                "seat_holders": holders}

    def setUp(self):
        import tempfile, pathlib
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._dir.name) / "clan.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_a_wallet_holding_a_seat_applies(self):
        reg = self._registry(self.tmp)
        self.assertIn("applyClan",
                      self._actions("wallet-07", self._ctx(reg, ["wallet-07"])))

    def test_a_wallet_outside_the_seat_ranking_does_not_apply(self):
        """The other twenty would otherwise queue applications forever."""
        reg = self._registry(self.tmp)
        self.assertNotIn("applyClan",
                         self._actions("wallet-29", self._ctx(reg, ["wallet-07"])))

    def test_no_seat_ranking_supplied_falls_back_to_applying(self):
        """Older runners pass no ranking; joining must not silently stop."""
        reg = self._registry(self.tmp)
        ctx = self._ctx(reg, None)
        ctx.pop("seat_holders")
        self.assertIn("applyClan", self._actions("wallet-29", ctx))

    def test_a_full_clan_admits_nobody(self):
        reg = self._registry(self.tmp)
        ctx = self._ctx(reg, [])
        self.assertNotIn("applyClan", self._actions("wallet-07", ctx))


class LeaderSeatTests(unittest.TestCase):
    """A leader with more applicants than seats admits the strongest first."""

    def test_the_highest_level_applicant_is_admitted_first(self):
        apps = [
            {"applicationId": "a1", "clanId": "c1", "userId": "solana:LOW",
             "status": "pending"},
            {"applicationId": "a2", "clanId": "c1", "userId": "solana:HIGH",
             "status": "pending"},
        ]
        ranked = clan.acceptable_applications(
            apps, "c1", {"solana:LOW", "solana:HIGH"},
            levels={"solana:LOW": 4, "solana:HIGH": 15})
        self.assertEqual([a["applicationId"] for a in ranked], ["a2", "a1"])

    def test_without_levels_the_queue_order_is_kept(self):
        apps = [
            {"applicationId": "a1", "clanId": "c1", "userId": "solana:A",
             "status": "pending"},
            {"applicationId": "a2", "clanId": "c1", "userId": "solana:B",
             "status": "pending"},
        ]
        ranked = clan.acceptable_applications(apps, "c1", {"solana:A", "solana:B"})
        self.assertEqual([a["applicationId"] for a in ranked], ["a1", "a2"])


class WeeklyQuestTests(unittest.TestCase):
    """The clan quest is the only measured way to buy seats, so the leader starts it."""

    from slcw.config import Config as _Config

    def _actions(self, ctx, config=None):
        from tests.test_orchestrator import make, FakeApi
        from slcw.model import parse_player
        orch = make(config=config or self._Config(enabled=True, dry_run=False),
                    api=FakeApi())
        state = parse_player({
            "level": 12, "energy": 80, "maxEnergy": 100, "balance": 5_000,
            "currentHealth": 130, "currentMana": 130, "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 13)),
            "newbieQuest": 999, "activity": None, "clanId": "c1",
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
        })
        return [c.action for c in orch.build_candidates(
            state, holdings={}, include_travel=False, wallet_id="wallet-01",
            clan_context=ctx)]

    def _ctx(self, role="leader", quest=None):
        return {"membership": clan.ClanMembership(clan_id="c1", role=role),
                "quest": quest, "registry": None, "fleet_uids": set(),
                "applications": [], "clan_info": None, "seat_holders": []}

    def test_a_leader_with_no_active_quest_starts_one(self):
        self.assertIn("generateClanQuest", self._actions(self._ctx()))

    def test_a_leader_with_a_quest_running_does_not_start_another(self):
        quest = clan.parse_quest({
            "requirements": [{"itemId": "frogslime", "required": 2000,
                              "collected": 0}],
            "rewardClanXp": 3500, "completedAt": None}, quest_id="q1")
        self.assertNotIn("generateClanQuest", self._actions(self._ctx(quest=quest)))

    def test_a_plain_member_never_starts_the_quest(self):
        self.assertNotIn("generateClanQuest",
                         self._actions(self._ctx(role="member")))

    def test_the_quest_stops_with_the_clan_feature(self):
        cfg = self._Config(enabled=True, dry_run=False, clan_enabled=False)
        self.assertNotIn("generateClanQuest",
                         self._actions(self._ctx(), config=cfg))


class ClanReachableUnderTasksTests(unittest.TestCase):
    """The hunt chain never runs out, so anything ordered after it never runs.

    Found live on 2026-08-21: wallet-01 banked 20,314 gold with auto-found armed
    and kept picking task battles instead. `createClan` was not merely losing the
    ranking — it was never built as a candidate, because the task branch returns
    before the clan branch is reached and a wallet in the Borderlands always has
    an eligible task. Every other clan action was unreachable the same way.
    """

    from slcw.config import Config as _Config

    def _task_status(self, status="active", progress=2):
        from slcw.tasks import Task, TaskStatus
        return TaskStatus(
            player_level=13, completed_count=4, all_done=False,
            has_active_task=True,
            task=Task(index=5, monster_id="bigfrog_lvl13_2", monster_level=13,
                      kills_required=8, kills_progress=progress, status=status,
                      gold_reward=1_300))

    def _state(self, gold):
        from slcw.model import parse_player
        return parse_player({
            "level": 13, "energy": 96, "maxEnergy": 100, "balance": gold,
            "currentHealth": 200, "maxHealth": 200, "currentMana": 130,
            "currentLocationId": "farm_3",
            "attributes": {"wisdom": 3, "vitality": 3},
            "claimedInitialRewardsV2": list(range(1, 20)),
            "newbieQuest": 999, "activity": None,
            "freeEnergyRefillsToday": 3, "lastFreeEnergyRefillDate": "2099-01-01",
        })

    def _actions(self, clan_ctx, gold=20_314, task_status=None, config=None):
        from tests.test_orchestrator import make, FakeApi
        orch = make(config=config or self._Config(
            enabled=True, dry_run=False, clan_auto_found=True,
            clan_founder_wallet="wallet-01", clan_name="RY Group",
            clan_tag="RYG"), api=FakeApi())
        return [c.action for c in orch.build_candidates(
            self._state(gold), holdings={}, include_travel=False,
            wallet_id="wallet-01", clan_context=clan_ctx,
            task_status=task_status if task_status is not None
            else self._task_status())]

    def setUp(self):
        import tempfile, pathlib
        self._dir = tempfile.TemporaryDirectory()
        self.registry = clan.ClanRegistry(
            path=pathlib.Path(self._dir.name) / "clan.json")

    def tearDown(self):
        self._dir.cleanup()

    def _ctx(self, **over):
        ctx = {"membership": None, "quest": None, "registry": self.registry,
               "fleet_uids": set(), "applications": [], "clan_info": None,
               "seat_holders": None, "levels_by_uid": {}}
        ctx.update(over)
        return ctx

    def test_founding_beats_the_endless_task_chain(self):
        self.assertEqual(self._actions(self._ctx())[0], "createClan")

    def test_a_finished_task_is_still_claimed_first(self):
        """Claiming is free gold and instant; it makes founding likelier, not later."""
        actions = self._actions(self._ctx(),
                                task_status=self._task_status(status="completed"))
        self.assertEqual(actions[0], "claimTaskReward")

    def test_joining_beats_the_task_chain_too(self):
        self.registry.record_clan("c1", "wallet-99")
        ctx = self._ctx(clan_info=clan.parse_clan("c1", {
            "level": 1, "maxMembers": 10, "memberCount": 1}))
        self.assertEqual(self._actions(ctx)[0], "applyClan")

    def test_the_task_chain_still_runs_when_no_clan_action_is_due(self):
        self.assertEqual(self._actions(self._ctx(), gold=500)[0], "startTaskBattle")
