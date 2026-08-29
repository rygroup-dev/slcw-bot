"""Telegram HTTP client built for low latency.

The previous implementation opened a fresh TLS connection for every call through
urllib. Measured against api.telegram.org that cost a median of 519 ms per request
— and occasionally stalled for 20 seconds — where a kept-alive session answers in
172 ms. A single button press makes two calls, so the handshake alone was adding
roughly 700 ms of dead time to every interaction.

This client keeps one connection alive, separates the long-poll from request
sending so a slow handler can never delay the next update, and retries transient
failures without collapsing the connection pool.
"""
from __future__ import annotations

import json
import threading
import time

from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt

API_ROOT = "https://api.telegram.org/bot"

# Resolve Telegram over IPv4 only. On 2026-08-29 this host began answering
# AF_UNSPEC lookups for api.telegram.org with an AAAA record and nothing else,
# and the route to that address is a black hole: the connect neither completes
# nor refuses, so every poll burned its full 65 second timeout and the control
# plane went silent. With no A record in the answer curl had nothing to fall
# back to. IPv6 to other hosts still worked, so the fault was invisible from
# outside. Telegram's IPv4 endpoint answers in half a second.
IPRESOLVE_V4 = 1

# Long-poll window. Telegram returns the moment an update arrives, so a longer
# window costs nothing and simply reduces idle request churn.
POLL_TIMEOUT = 50

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class TelegramError(RuntimeError):
    def __init__(self, message: str, status: int = 0, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class TelegramClient:
    """Keep-alive Telegram transport.

    Polling and sending use separate sessions: a long poll occupies its
    connection for up to a minute, and sharing that with outbound calls would
    serialise every reply behind it.
    """

    # Floor for uploads, which carry a payload rather than a short form post.
    timeout_floor: float = 15.0

    def __init__(self, token: str):
        self.token = token
        resolve = {"curl_options": {CurlOpt.IPRESOLVE: IPRESOLVE_V4}}
        self._send_session = cffi.Session(**resolve)
        self._poll_session = cffi.Session(**resolve)
        self._lock = threading.Lock()
        self.calls = 0
        self.total_latency = 0.0

    @property
    def average_latency_ms(self) -> float:
        return (self.total_latency / self.calls * 1000) if self.calls else 0.0

    def _url(self, method: str) -> str:
        return f"{API_ROOT}{self.token}/{method}"

    def call(self, method: str, payload: dict | None = None,
             timeout: float = 15.0, attempts: int = 3) -> dict:
        """Send a request on the keep-alive session."""
        started = time.perf_counter()
        last: Exception | None = None

        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2.0, 0.4 * (2 ** attempt)))
            try:
                with self._lock:
                    response = self._send_session.post(
                        self._url(method), data=payload or {}, timeout=timeout)
            except Exception as exc:
                last = TelegramError(f"{type(exc).__name__}: {exc}")
                continue

            if response.status_code in RETRY_STATUS:
                retry_after = 0
                try:
                    retry_after = int((response.json() or {}).get(
                        "parameters", {}).get("retry_after", 0))
                except Exception:
                    pass
                if retry_after:
                    time.sleep(min(retry_after, 5))
                last = TelegramError(f"HTTP {response.status_code}",
                                     status=response.status_code,
                                     retry_after=retry_after)
                continue

            elapsed = time.perf_counter() - started
            self.calls += 1
            self.total_latency += elapsed

            try:
                return response.json() or {}
            except Exception:
                return {}

        raise last or TelegramError("request failed with no diagnostic")

    def call_quietly(self, method: str, payload: dict | None = None) -> dict:
        """Best-effort call. Used where a failure must never break a handler.

        "message is not modified" and expired-callback errors are routine and
        carry no information worth surfacing.
        """
        try:
            return self.call(method, payload, attempts=1)
        except Exception:
            return {}

    def upload(self, method: str, payload: dict, filename: str,
               content: str) -> dict:
        """Send a multipart request carrying an in-memory file.

        Used for wallet exports: a document keeps key material out of chat
        previews and notifications, where a plain message would put it.
        """
        try:
            with self._lock:
                response = self._send_session.post(
                    self._url(method),
                    data=payload,
                    files={"document": (filename, content.encode("utf-8"),
                                        "application/json")},
                    timeout=max(self.timeout_floor, 60.0),
                )
        except Exception as exc:
            raise TelegramError(f"upload failed: {type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise TelegramError(f"upload HTTP {response.status_code}",
                                status=response.status_code)
        try:
            return response.json() or {}
        except Exception:
            return {}

    def poll(self, offset: int) -> list:
        """Long-poll for updates on the dedicated polling connection."""
        try:
            response = self._poll_session.post(
                self._url("getUpdates"),
                data={"timeout": POLL_TIMEOUT, "offset": offset,
                      "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=POLL_TIMEOUT + 15,
            )
        except Exception as exc:
            raise TelegramError(f"poll failed: {type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise TelegramError(f"poll HTTP {response.status_code}",
                                status=response.status_code)
        try:
            return (response.json() or {}).get("result", []) or []
        except Exception:
            return []

    def close(self) -> None:
        for session in (self._send_session, self._poll_session):
            try:
                session.close()
            except Exception:
                pass
