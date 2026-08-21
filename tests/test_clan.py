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
