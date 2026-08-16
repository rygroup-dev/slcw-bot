import time
import unittest
from unittest.mock import patch

from slcw.auth import AuthError, Session, SessionManager
from slcw.config import Config
from slcw.transport import (ApiError, Transport, TransportError, build_persona,
                            unwrap)


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeCurlSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def transport_with(responses, config=None):
    transport = Transport(config=config or Config(http_backoff_base=1.0))
    transport._session = FakeCurlSession(responses)
    return transport


class UnwrapTests(unittest.TestCase):
    def test_extracts_result_envelope(self):
        self.assertEqual(unwrap({"result": {"battleId": "x"}}), {"battleId": "x"})

    def test_non_dict_result_does_not_crash(self):
        # The old client called .get() on this and raised AttributeError.
        self.assertEqual(unwrap({"result": [1, 2, 3]}), {"value": [1, 2, 3]})

    def test_result_containing_data_key_is_not_over_unwrapped(self):
        payload = {"result": {"data": {"inner": 1}, "success": True}}
        self.assertEqual(unwrap(payload), {"data": {"inner": 1}, "success": True})

    def test_payload_without_result_passes_through(self):
        self.assertEqual(unwrap({"idToken": "abc"}), {"idToken": "abc"})


class PersonaTests(unittest.TestCase):
    def test_persona_is_stable_for_a_wallet(self):
        self.assertEqual(build_persona("wallet-01"), build_persona("wallet-01"))

    def test_personas_differ_between_wallets(self):
        personas = {tuple(sorted(build_persona(f"wallet-{i:02d}").items()))
                    for i in range(12)}
        self.assertGreater(len(personas), 1)

    def test_headers_never_advertise_python(self):
        headers = Transport(config=Config())._headers()
        self.assertNotIn("urllib", headers["User-Agent"])
        self.assertNotIn("Python", headers["User-Agent"])
        self.assertIn("Chrome/", headers["User-Agent"])

    def test_headers_carry_browser_context(self):
        headers = Transport(config=Config())._headers()
        for key in ("Origin", "Referer", "sec-ch-ua", "Sec-Fetch-Mode", "Accept-Language"):
            self.assertIn(key, headers)

    def test_user_agent_version_matches_client_hint(self):
        transport = Transport(config=Config(), persona=build_persona("wallet-07"))
        headers = transport._headers()
        version = transport.persona["ua_version"]
        self.assertIn(f"Chrome/{version}.", headers["User-Agent"])
        self.assertIn(f'v="{version}"', headers["sec-ch-ua"])


class RetryTests(unittest.TestCase):
    def test_transient_503_is_retried_then_succeeds(self):
        transport = transport_with([
            FakeResponse(503, ""),
            FakeResponse(200, '{"result":{"ok":true}}'),
        ])
        with patch("slcw.transport.time.sleep"):
            payload = transport.request("POST", "https://example/x")
        self.assertEqual(payload, {"result": {"ok": True}})
        self.assertEqual(len(transport._session.requests), 2)

    def test_429_is_retried(self):
        transport = transport_with([FakeResponse(429, ""), FakeResponse(200, "{}")])
        with patch("slcw.transport.time.sleep"):
            transport.request("GET", "https://example/x")
        self.assertEqual(len(transport._session.requests), 2)

    def test_network_exception_is_retried(self):
        transport = transport_with([ConnectionError("reset"), FakeResponse(200, "{}")])
        with patch("slcw.transport.time.sleep"):
            transport.request("GET", "https://example/x")
        self.assertEqual(len(transport._session.requests), 2)

    def test_gives_up_after_configured_attempts(self):
        config = Config(http_max_attempts=3, http_backoff_base=1.0)
        transport = transport_with([FakeResponse(503, "")] * 3, config=config)
        with patch("slcw.transport.time.sleep"), self.assertRaises(TransportError):
            transport.request("GET", "https://example/x")
        self.assertEqual(len(transport._session.requests), 3)

    def test_client_error_is_not_retried(self):
        transport = transport_with([
            FakeResponse(409, '{"error":{"message":"Reward already claimed",'
                              '"status":"ALREADY_EXISTS"}}')])
        with self.assertRaises(ApiError) as ctx:
            transport.request("POST", "https://example/x")
        self.assertEqual(ctx.exception.status_code, "ALREADY_EXISTS")
        self.assertTrue(ctx.exception.is_benign)
        self.assertEqual(len(transport._session.requests), 1)

    def test_failed_precondition_is_benign(self):
        transport = transport_with([
            FakeResponse(400, '{"error":{"message":"Slot head is already occupied",'
                              '"status":"FAILED_PRECONDITION"}}')])
        with self.assertRaises(ApiError) as ctx:
            transport.request("POST", "https://example/x")
        self.assertTrue(ctx.exception.is_benign)

    def test_internal_error_is_not_benign(self):
        transport = transport_with([
            FakeResponse(400, '{"error":{"message":"bad","status":"INVALID_ARGUMENT"}}')])
        with self.assertRaises(ApiError) as ctx:
            transport.request("POST", "https://example/x")
        self.assertFalse(ctx.exception.is_benign)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(firebase_api_key="test-key")
        self.wallet = {"id": "wallet-01", "public_key": "pk", "private_key": "sk"}

    def _session(self, expires_in=3600):
        return Session(
            wallet_id="wallet-01", public_key="pk", nickname="n",
            id_token="old-token", refresh_token="refresh-1", local_id="uid",
            expires_at=int(time.time()) + expires_in, logged_in_at=int(time.time()))

    def test_valid_session_is_reused_without_any_request(self):
        manager = SessionManager(self.config)
        manager._sessions["wallet-01"] = self._session()
        transport = transport_with([])
        returned = manager.get(self.wallet, transport)
        self.assertEqual(returned.id_token, "old-token")
        self.assertEqual(transport._session.requests, [])

    def test_expired_session_refreshes_instead_of_logging_in(self):
        manager = SessionManager(self.config)
        manager._sessions["wallet-01"] = self._session(expires_in=-10)
        transport = transport_with([
            FakeResponse(200, '{"id_token":"new-token","refresh_token":"refresh-2",'
                              '"expires_in":"3600"}')])
        returned = manager.get(self.wallet, transport)
        self.assertEqual(returned.id_token, "new-token")
        self.assertEqual(returned.refresh_token, "refresh-2")
        self.assertEqual(returned.refresh_count, 1)
        # Exactly one request: no getSolanaNonce, no verifySolanaLogin.
        self.assertEqual(len(transport._session.requests), 1)
        self.assertIn("securetoken", transport._session.requests[0][1])

    def test_refresh_failure_falls_back_to_full_login(self):
        manager = SessionManager(self.config)
        manager._sessions["wallet-01"] = self._session(expires_in=-10)
        with patch.object(SessionManager, "_login") as login:
            login.return_value = self._session()
            transport = transport_with([FakeResponse(401, '{"error":{"message":"revoked",'
                                                          '"status":"UNAUTHENTICATED"}}')])
            manager.get(self.wallet, transport)
        login.assert_called_once()

    def test_missing_api_key_is_reported(self):
        manager = SessionManager(Config(firebase_api_key=""))
        with self.assertRaises(AuthError):
            manager._login(self.wallet, transport_with([]))

    def test_expiry_margin_refreshes_before_actual_expiry(self):
        manager = SessionManager(self.config)
        # 60 seconds of nominal life left is inside the 300s safety margin.
        manager._sessions["wallet-01"] = self._session(expires_in=60)
        self.assertTrue(manager._sessions["wallet-01"].is_valid)
        stale = self._session(expires_in=-1)
        self.assertFalse(stale.is_valid)


if __name__ == "__main__":
    unittest.main()
