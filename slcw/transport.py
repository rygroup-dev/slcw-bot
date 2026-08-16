"""HTTP transport with browser impersonation, per-wallet persona, and backoff.

The previous client used urllib, which advertises `User-Agent: Python-urllib/3.13`
and a Python TLS handshake. curl_cffi reproduces Chrome's TLS/JA3 fingerprint, and
each wallet keeps a sticky persona so a given account always presents the same
browser rather than a new one every request.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field

from curl_cffi import requests as cffi

from . import guardrails
from .config import APP_ORIGIN, Config

# curl_cffi impersonation targets paired with matching client-hint headers. Keeping
# these consistent matters: a Chrome 131 JA3 with a Chrome 99 UA is its own signal.
PERSONAS = (
    {"impersonate": "chrome124", "ua_version": "124", "platform": "Windows", "platform_full": '"Windows"'},
    {"impersonate": "chrome120", "ua_version": "120", "platform": "Windows", "platform_full": '"Windows"'},
    {"impersonate": "chrome123", "ua_version": "123", "platform": "macOS", "platform_full": '"macOS"'},
    {"impersonate": "chrome119", "ua_version": "119", "platform": "Windows", "platform_full": '"Windows"'},
    {"impersonate": "chrome116", "ua_version": "116", "platform": "Linux", "platform_full": '"Linux"'},
)

LANGUAGES = ("en-US,en;q=0.9", "en-GB,en;q=0.9", "id-ID,id;q=0.9,en;q=0.8", "en-US,en;q=0.9,id;q=0.8")

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransportError(RuntimeError):
    """Network or HTTP failure that survived all retries."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class ApiError(RuntimeError):
    """Structured server rejection, e.g. ALREADY_EXISTS or FAILED_PRECONDITION.

    The old engine matched errors by searching for the substring "already claimed",
    but the server actually returns status "ALREADY_EXISTS" with the message
    "Reward already claimed", so the match never fired and normal, expected
    rejections were escalated into circuit-breaker trips.
    """

    def __init__(self, message: str, status_code: str = "", http_status: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.http_status = http_status

    @property
    def is_benign(self) -> bool:
        """True when the rejection means 'already done', not 'something broke'."""
        return self.status_code in ("ALREADY_EXISTS", "FAILED_PRECONDITION")


def build_persona(seed: str) -> dict:
    """Deterministic persona for a wallet id, so it never changes between runs."""
    rng = random.Random(f"slcw-persona-{seed}")
    base = dict(rng.choice(PERSONAS))
    base["accept_language"] = rng.choice(LANGUAGES)
    return base


@dataclass
class Transport:
    config: Config
    persona: dict = field(default_factory=lambda: build_persona("default"))
    proxy: str | None = None
    _session: object | None = field(default=None, init=False, repr=False)

    @classmethod
    def for_wallet(cls, config: Config, wallet: dict) -> "Transport":
        persona = wallet.get("persona") or build_persona(wallet.get("id", "default"))
        return cls(config=config, persona=persona, proxy=wallet.get("proxy") or None)

    def _headers(self, extra: dict | None = None) -> dict:
        version = self.persona["ua_version"]
        platform = self.persona["platform"]
        ua_platform = {
            "Windows": "Windows NT 10.0; Win64; x64",
            "macOS": "Macintosh; Intel Mac OS X 10_15_7",
            "Linux": "X11; Linux x86_64",
        }[platform]
        headers = {
            "User-Agent": (
                f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version}.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": self.persona["accept_language"],
            "Content-Type": "application/json",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "sec-ch-ua": f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not?A_Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.persona["platform_full"],
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
        headers.update(extra or {})
        return headers

    @property
    def session(self):
        if self._session is None:
            kwargs = {"impersonate": self.persona["impersonate"]}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = cffi.Session(**kwargs)
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def request(self, method: str, url: str, *, json_body: dict | None = None,
                headers: dict | None = None) -> dict:
        """Perform a request, retrying transient failures with exponential backoff.

        Retrying here rather than in the engine is what keeps a single 503 from
        incrementing the wallet's consecutive-error count and tripping the breaker.
        """
        attempts = max(1, self.config.http_max_attempts)
        last_error: Exception | None = None

        for attempt in range(attempts):
            if attempt:
                delay = (self.config.http_backoff_base ** attempt) + random.uniform(0, 1.2)
                time.sleep(delay)
            try:
                response = self.session.request(
                    method,
                    url,
                    json=json_body,
                    headers=self._headers(headers),
                    timeout=self.config.http_timeout_seconds,
                )
            except Exception as exc:  # curl-level failure: DNS, TLS, proxy, timeout
                last_error = TransportError(f"{type(exc).__name__}: {exc}")
                continue

            if response.status_code in RETRY_STATUS:
                last_error = TransportError(
                    f"HTTP {response.status_code}", status=response.status_code)
                continue

            return self._parse(response)

        raise last_error or TransportError("request failed with no diagnostic")

    @staticmethod
    def _parse(response) -> dict | list:
        try:
            payload = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            if response.status_code >= 400:
                raise TransportError(
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    status=response.status_code) from None
            return {}

        # Firestore's :runQuery answers with a JSON array of result frames rather
        # than an object, so error extraction only applies to object payloads.
        if isinstance(payload, list):
            for frame in payload:
                if isinstance(frame, dict) and isinstance(frame.get("error"), dict):
                    error = frame["error"]
                    raise ApiError(
                        error.get("message", "unknown server error"),
                        status_code=str(error.get("status", "")),
                        http_status=response.status_code,
                    )
            if response.status_code >= 400:
                raise TransportError(
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    status=response.status_code)
            return payload

        error = payload.get("error")
        if isinstance(error, dict):
            raise ApiError(
                error.get("message", "unknown server error"),
                status_code=str(error.get("status", "")),
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise TransportError(
                f"HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code)
        return payload

    def call_function(self, name: str, data: dict | None = None,
                      id_token: str | None = None) -> dict:
        """Invoke a Firebase callable. Every call passes the guardrail allowlist."""
        guardrails.check(name)
        from .config import FUNCTION_BASE

        headers = {"Authorization": f"Bearer {id_token}"} if id_token else {}
        payload = self.request(
            "POST", f"{FUNCTION_BASE}/{name}",
            json_body={"data": data or {}}, headers=headers,
        )
        return unwrap(payload)


def unwrap(payload: dict) -> dict:
    """Extract the callable's result envelope.

    Firebase callables answer `{"result": <data>}`. The old client chained
    `.get("result", result).get("data", ...)`, which raised AttributeError whenever
    the result was not a dict and silently unwrapped one level too far whenever the
    result happened to contain its own "data" key.
    """
    if not isinstance(payload, dict):
        return {"value": payload}
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return {"value": result}
    return result
