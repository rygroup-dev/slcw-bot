"""The discard digest.

Destroying junk is routine and constant; the alert channel is not. These tests
pin the shape that keeps both true — one message per window, and never at the
cost of the audit trail on disk.
"""
import unittest

from slcw import notify


class RecordingNotifier(notify.Notifier):
    def __init__(self):
        super().__init__("token", "chat")
        self.sent = []

    def send(self, text, **extra):
        self.sent.append(text)
        return {"ok": True}


class DiscardDigestTests(unittest.TestCase):
    def setUp(self):
        self.notifier = RecordingNotifier()
        self.alerts = notify.Alerts(self.notifier)

    def test_a_destroyed_stack_does_not_message_immediately(self):
        self.alerts.discarded("wallet-01", "frostpelt", 1)
        self.assertEqual(self.notifier.sent, [])

    def test_the_window_closing_sends_exactly_one_message(self):
        for index in range(25):
            self.alerts.discarded(f"wallet-{index:02d}", "frostpelt", 1)
        self.alerts._discard_since -= notify.DISCARD_DIGEST_S + 1
        self.alerts.flush_discards()
        self.assertEqual(len(self.notifier.sent), 1)

    def test_the_digest_counts_stacks_items_and_wallets(self):
        self.alerts.discarded("wallet-01", "frostpelt", 1)
        self.alerts.discarded("wallet-01", "lifevine", 2)
        self.alerts.discarded("wallet-02", "frostpelt", 3)
        self.alerts.flush_discards(force=True)
        message = self.notifier.sent[0]
        self.assertIn("3 tumpukan", message)
        self.assertIn("6 item", message)
        self.assertIn("2 wallet", message)
        self.assertIn("frostpelt ×2", message)
        self.assertIn("lifevine ×1", message)

    def test_the_digest_points_at_the_audit_trail(self):
        # The message is a summary; the file is the record. An operator who
        # wants to know exactly what was destroyed has to be told where to look.
        self.alerts.discarded("wallet-01", "frostpelt", 1)
        self.alerts.flush_discards(force=True)
        self.assertIn("discards.jsonl", self.notifier.sent[0])

    def test_many_kinds_are_summarised_rather_than_listed_in_full(self):
        for index in range(20):
            self.alerts.discarded("wallet-01", f"junk{index:02d}", 1)
        self.alerts.flush_discards(force=True)
        self.assertIn("jenis lain", self.notifier.sent[0])
        self.assertLess(len(self.notifier.sent[0]), 500)

    def test_flushing_an_empty_buffer_says_nothing(self):
        self.alerts.flush_discards(force=True)
        self.alerts.flush_discards()
        self.assertEqual(self.notifier.sent, [])

    def test_a_flush_empties_the_buffer_so_nothing_is_reported_twice(self):
        self.alerts.discarded("wallet-01", "frostpelt", 1)
        self.alerts.flush_discards(force=True)
        self.alerts.flush_discards(force=True)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_a_new_window_opens_after_a_flush(self):
        self.alerts.discarded("wallet-01", "frostpelt", 1)
        self.alerts.flush_discards(force=True)
        self.alerts.discarded("wallet-02", "lifevine", 1)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_failures_that_need_the_operator_are_still_immediate(self):
        # The whole point of holding discards back is that the channel stays
        # worth reading when something actually breaks.
        self.alerts.circuit_breaker("wallet-01", "IronVale82", "boom")
        self.assertEqual(len(self.notifier.sent), 1)


if __name__ == "__main__":
    unittest.main()
