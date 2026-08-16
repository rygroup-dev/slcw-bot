import unittest

from slcw import guardrails
from slcw.config import Config
from slcw.transport import Transport


class GuardrailTests(unittest.TestCase):
    def test_allows_known_safe_callable(self):
        guardrails.check("finishActivity")  # must not raise

    def test_denies_diamond_spending(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("spendDiamonds")

    def test_denies_withdrawal(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("withdraw")

    def test_denies_transaction_signing(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("signTransaction")

    def test_denies_market_order_creation(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("createOrder")

    def test_denies_speedup(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("speedUp")

    def test_denies_skip_activity_time(self):
        # The frontend charges 5 * ceil(seconds_left / 60) diamonds for this.
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("skipActivityTime")

    def test_denies_city_entry_fee(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("payCityEntryFee")

    def test_denies_arena_queue(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("joinArenaQueue")

    def test_denies_buy_level(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("buyLevel")

    def test_allows_gathering(self):
        guardrails.check("startFarming")

    def test_unknown_callable_denied_by_default(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("someBrandNewCallable")

    def test_allowlist_and_denylist_never_overlap(self):
        self.assertEqual(guardrails.ALLOWED_CALLABLES & guardrails.DENIED_CALLABLES, set())

    def test_is_allowed_helper(self):
        self.assertTrue(guardrails.is_allowed("startRelax"))
        self.assertFalse(guardrails.is_allowed("withdraw"))


class TransportEnforcementTests(unittest.TestCase):
    """The guard must sit in the transport, not only in the caller."""

    def test_denied_callable_never_reaches_the_network(self):
        transport = Transport(config=Config())
        called = []
        transport.request = lambda *a, **k: called.append(a)  # type: ignore

        with self.assertRaises(guardrails.GuardrailViolation):
            transport.call_function("withdraw", {}, "token")
        self.assertEqual(called, [], "a denied callable must not issue a request")


if __name__ == "__main__":
    unittest.main()
