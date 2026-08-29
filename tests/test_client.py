"""Tests for the low-latency Telegram transport.

Nothing here touches the network: the curl sessions are replaced with fakes that
record what was asked of them.
"""
import unittest
from unittest.mock import patch

from curl_cffi.const import CurlOpt

from bot import client as client_mod
from bot.client import POLL_TIMEOUT, TelegramClient, TelegramError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def client_with(send=None, poll=None) -> TelegramClient:
    client = TelegramClient("test-token")
    client._send_session = send or FakeSession()
    client._poll_session = poll or FakeSession()
    return client


class RoutingTests(unittest.TestCase):
    def test_token_is_embedded_in_the_url(self):
        client = client_with(FakeSession(FakeResponse(200, {"ok": True})))
        client.call("getMe")
        url = client._send_session.requests[0][0]
        self.assertIn("test-token", url)
        self.assertTrue(url.endswith("/getMe"))

    def test_payload_is_forwarded(self):
        client = client_with(FakeSession(FakeResponse(200, {"ok": True})))
        client.call("sendMessage", {"chat_id": 42, "text": "hi"})
        self.assertEqual(client._send_session.requests[0][1]["data"],
                         {"chat_id": 42, "text": "hi"})

    def test_json_response_is_returned(self):
        client = client_with(FakeSession(FakeResponse(200, {"result": [1, 2]})))
        self.assertEqual(client.call("getUpdates"), {"result": [1, 2]})

    def test_unparseable_body_yields_an_empty_dict(self):
        client = client_with(FakeSession(FakeResponse(200, None, "not json")))
        self.assertEqual(client.call("getMe"), {})


class SessionSeparationTests(unittest.TestCase):
    """A 50-second long poll must never occupy the connection used for replies."""

    def test_poll_and_send_use_different_sessions(self):
        client = TelegramClient("t")
        self.assertIsNot(client._send_session, client._poll_session)

    def test_sending_does_not_touch_the_poll_session(self):
        send, poll = FakeSession(FakeResponse(200, {"ok": True})), FakeSession()
        client = client_with(send, poll)
        client.call("sendMessage", {"chat_id": 1})
        self.assertEqual(len(send.requests), 1)
        self.assertEqual(poll.requests, [])

    def test_polling_does_not_touch_the_send_session(self):
        send = FakeSession()
        poll = FakeSession(FakeResponse(200, {"result": [{"update_id": 1}]}))
        client = client_with(send, poll)
        self.assertEqual(client.poll(0), [{"update_id": 1}])
        self.assertEqual(send.requests, [])


class RetryTests(unittest.TestCase):
    def test_transient_status_is_retried_then_succeeds(self):
        session = FakeSession(FakeResponse(503, {}), FakeResponse(200, {"ok": True}))
        client = client_with(session)
        with patch("bot.client.time.sleep"):
            self.assertEqual(client.call("getMe"), {"ok": True})
        self.assertEqual(len(session.requests), 2)

    def test_network_exception_is_retried(self):
        session = FakeSession(ConnectionError("reset"), FakeResponse(200, {"ok": True}))
        client = client_with(session)
        with patch("bot.client.time.sleep"):
            client.call("getMe")
        self.assertEqual(len(session.requests), 2)

    def test_gives_up_after_the_attempt_budget(self):
        session = FakeSession(*[FakeResponse(500, {})] * 3)
        client = client_with(session)
        with patch("bot.client.time.sleep"), self.assertRaises(TelegramError):
            client.call("getMe", attempts=3)
        self.assertEqual(len(session.requests), 3)

    def test_rate_limit_honours_retry_after(self):
        session = FakeSession(
            FakeResponse(429, {"parameters": {"retry_after": 3}}),
            FakeResponse(200, {"ok": True}))
        client = client_with(session)
        with patch("bot.client.time.sleep") as sleep:
            client.call("getMe")
        self.assertIn(3, [call.args[0] for call in sleep.call_args_list])

    def test_client_errors_are_not_retried(self):
        session = FakeSession(FakeResponse(400, {"ok": False, "description": "bad"}))
        client = client_with(session)
        self.assertEqual(client.call("getMe"), {"ok": False, "description": "bad"})
        self.assertEqual(len(session.requests), 1)

    def test_call_quietly_swallows_failures(self):
        client = client_with(FakeSession(ConnectionError("down")))
        self.assertEqual(client.call_quietly("editMessageText", {}), {})

    def test_call_quietly_does_not_retry(self):
        session = FakeSession(ConnectionError("down"))
        client = client_with(session)
        client.call_quietly("getMe")
        self.assertEqual(len(session.requests), 1)


class PollTests(unittest.TestCase):
    def test_long_poll_window_is_sent(self):
        poll = FakeSession(FakeResponse(200, {"result": []}))
        client = client_with(poll=poll)
        client.poll(offset=7)
        data = poll.requests[0][1]["data"]
        self.assertEqual(data["timeout"], POLL_TIMEOUT)
        self.assertEqual(data["offset"], 7)

    def test_only_the_update_types_we_handle_are_requested(self):
        poll = FakeSession(FakeResponse(200, {"result": []}))
        client = client_with(poll=poll)
        client.poll(0)
        self.assertIn("callback_query", poll.requests[0][1]["data"]["allowed_updates"])
        self.assertNotIn("edited_message",
                         poll.requests[0][1]["data"]["allowed_updates"])

    def test_read_timeout_exceeds_the_poll_window(self):
        """A timeout shorter than the poll would abort every idle poll."""
        poll = FakeSession(FakeResponse(200, {"result": []}))
        client = client_with(poll=poll)
        client.poll(0)
        self.assertGreater(poll.requests[0][1]["timeout"], POLL_TIMEOUT)

    def test_poll_failure_raises(self):
        client = client_with(poll=FakeSession(ConnectionError("dropped")))
        with self.assertRaises(TelegramError):
            client.poll(0)

    def test_non_200_poll_raises(self):
        client = client_with(poll=FakeSession(FakeResponse(502, {})))
        with self.assertRaises(TelegramError):
            client.poll(0)

    def test_malformed_poll_body_yields_no_updates(self):
        client = client_with(poll=FakeSession(FakeResponse(200, None, "garbage")))
        self.assertEqual(client.poll(0), [])


class LatencyTests(unittest.TestCase):
    def test_latency_is_tracked_for_successful_calls(self):
        client = client_with(FakeSession(FakeResponse(200, {}), FakeResponse(200, {})))
        client.call("getMe")
        client.call("getMe")
        self.assertEqual(client.calls, 2)
        self.assertGreater(client.average_latency_ms, 0)

    def test_average_is_zero_before_any_call(self):
        self.assertEqual(client_with().average_latency_ms, 0.0)

    def test_failed_calls_do_not_count(self):
        client = client_with(FakeSession(*[FakeResponse(500, {})] * 2))
        with patch("bot.client.time.sleep"), self.assertRaises(TelegramError):
            client.call("getMe", attempts=2)
        self.assertEqual(client.calls, 0)


class AddressFamilyTests(unittest.TestCase):
    """2026-08-29: every poll hung for 65 seconds and Telegram went dead.

    The host resolved api.telegram.org to an AAAA record only, and the route to
    Telegram's IPv6 address is a black hole from this VPS — the connect never
    refuses, it just hangs, and with no A record in the answer curl had nothing
    to fall back to. IPv6 to other hosts was fine, so nothing looked broken.
    Asking curl for A records directly answered in half a second.
    """

    def test_sessions_resolve_over_ipv4(self):
        made = []

        def record(*args, **kwargs):
            made.append(kwargs)
            return FakeSession()

        with patch("bot.client.cffi.Session", side_effect=record):
            TelegramClient("token")

        self.assertEqual(len(made), 2, "both the send and poll sessions")
        for kwargs in made:
            self.assertEqual(kwargs["curl_options"][CurlOpt.IPRESOLVE],
                             client_mod.IPRESOLVE_V4)


if __name__ == "__main__":
    unittest.main()
