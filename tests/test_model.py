import unittest

from slcw.model import Activity, normalize_timestamp, parse_player


class TimestampTests(unittest.TestCase):
    def test_iso_string(self):
        self.assertEqual(normalize_timestamp("1970-01-01T00:00:01Z"), 1000)

    def test_iso_with_fraction(self):
        # 2026-08-16T05:15:28.661Z, the endTime of a live production activity.
        self.assertEqual(normalize_timestamp("2026-08-16T05:15:28.661Z"), 1786857328661)

    def test_firestore_seconds_map(self):
        self.assertEqual(normalize_timestamp({"seconds": 1786857095}), 1786857095000)

    def test_underscore_seconds_map(self):
        self.assertEqual(normalize_timestamp({"_seconds": 1786857095}), 1786857095000)

    def test_raw_milliseconds_pass_through(self):
        # startRelax returns endTime as raw milliseconds.
        self.assertEqual(normalize_timestamp(1786853586329), 1786853586329)

    def test_raw_seconds_are_promoted_to_milliseconds(self):
        # The old implementation returned this unchanged, dating the activity to
        # 1970 and making every activity look expired.
        self.assertEqual(normalize_timestamp(1786857095), 1786857095000)

    def test_garbage_is_zero(self):
        self.assertEqual(normalize_timestamp("not a date"), 0)
        self.assertEqual(normalize_timestamp(None), 0)
        self.assertEqual(normalize_timestamp(True), 0)


class ActivityTests(unittest.TestCase):
    def test_future_activity_is_not_expired(self):
        activity = Activity(end_ms=normalize_timestamp(9999999999))
        self.assertFalse(activity.is_expired)
        self.assertGreater(activity.seconds_remaining(), 0)

    def test_past_activity_is_expired(self):
        self.assertTrue(Activity(end_ms=1000).is_expired)

    def test_missing_end_time_is_not_expired(self):
        self.assertFalse(Activity(end_ms=0).is_expired)


class PlayerTests(unittest.TestCase):
    # Shape taken from a live capture.
    DOC = {
        "level": 6, "xp": 1398, "balance": 0, "premium_balance": 0, "usdt_balance": 0,
        "energy": 85, "maxEnergy": 100, "currentHealth": 80, "currentMana": 130,
        "attributePoints": 0, "newbieQuest": 7, "grade": 1,
        "currentLocationId": "farm_3",
        "attributes": {"wisdom": 3, "vitality": 3, "dexterity": 3, "might": 3,
                       "intelligence": 3},
        "claimedInitialRewardsV2": [1, 2, 3, 4, 5, 6],
        "activity": None,
    }

    def test_parses_core_fields(self):
        state = parse_player(self.DOC)
        self.assertEqual(state.level, 6)
        self.assertEqual(state.energy, 85)
        self.assertEqual(state.max_energy, 100)
        self.assertEqual(state.location_id, "farm_3")

    def test_max_health_matches_server_battle_document(self):
        # The battle document reports maxHP 130 for this character.
        self.assertEqual(parse_player(self.DOC).max_health, 130)

    def test_no_unclaimed_levels_when_all_claimed(self):
        self.assertEqual(parse_player(self.DOC).unclaimed_levels(), [])

    def test_unclaimed_levels_reported(self):
        doc = dict(self.DOC, claimedInitialRewardsV2=[1, 3])
        self.assertEqual(parse_player(doc).unclaimed_levels(), [2, 4, 5, 6])

    def test_busy_when_activity_running(self):
        doc = dict(self.DOC, activity={"type": "production", "endTime": {"seconds": 9999999999}})
        self.assertTrue(parse_player(doc).is_busy)

    def test_not_busy_when_activity_expired(self):
        doc = dict(self.DOC, activity={"type": "production", "endTime": {"seconds": 1}})
        state = parse_player(doc)
        self.assertFalse(state.is_busy)
        self.assertTrue(state.activity.is_expired)


if __name__ == "__main__":
    unittest.main()
