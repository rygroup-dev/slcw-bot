"""Session management: log in once, refresh thereafter.

The old engine called client.login() at the top of every cycle — a full
getSolanaNonce + verifySolanaLogin + Firebase exchange, 288 times per wallet per
day, while the refresh token it stored went unused. Real clients authenticate once
per session and refresh silently. This module does the same, which removes the most
distinctive pattern in the whole system and makes each cycle faster.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field

import base58
from solders.keypair import Keypair

from .config import IDENTITY_URL, SECURETOKEN_URL, Config
from .transport import ApiError, Transport, TransportError

# Refresh this many seconds before the token actually expires.
EXPIRY_MARGIN = 300


class AuthError(RuntimeError):
    pass


@dataclass
class Session:
    wallet_id: str
    public_key: str
    nickname: str
    id_token: str
    refresh_token: str
    local_id: str
    expires_at: int
    logged_in_at: int
    refresh_count: int = 0

    @property
    def is_valid(self) -> bool:
        return bool(self.id_token) and time.time() < self.expires_at


@dataclass
class SessionManager:
    """Caches one Session per wallet and keeps it alive by refresh."""

    config: Config
    _sessions: dict = field(default_factory=dict)

    def get(self, wallet: dict, transport: Transport) -> Session:
        wallet_id = wallet["id"]
        session = self._sessions.get(wallet_id)

        if session is not None and session.is_valid:
            return session

        if session is not None and session.refresh_token:
            try:
                return self._refresh(session, transport)
            except (TransportError, ApiError, AuthError):
                # Refresh tokens can be revoked server-side; fall through to a
                # full login rather than failing the whole cycle.
                pass

        session = self._login(wallet, transport)
        self._sessions[wallet_id] = session
        return session

    def invalidate(self, wallet_id: str) -> None:
        self._sessions.pop(wallet_id, None)

    def _refresh(self, session: Session, transport: Transport) -> Session:
        if not self.config.firebase_api_key:
            raise AuthError("SLCW_FIREBASE_API_KEY is unset")
        payload = transport.request(
            "POST",
            f"{SECURETOKEN_URL}?key={self.config.firebase_api_key}",
            json_body={"grant_type": "refresh_token", "refresh_token": session.refresh_token},
            headers={"Content-Type": "application/json"},
        )
        id_token = payload.get("id_token") or payload.get("idToken")
        if not id_token:
            raise AuthError("refresh returned no id_token")
        session.id_token = id_token
        session.refresh_token = payload.get("refresh_token") or session.refresh_token
        session.expires_at = int(time.time()) + int(payload.get("expires_in", 3600)) - EXPIRY_MARGIN
        session.refresh_count += 1
        self._sessions[session.wallet_id] = session
        return session

    def _login(self, wallet: dict, transport: Transport) -> Session:
        if not self.config.firebase_api_key:
            raise AuthError("SLCW_FIREBASE_API_KEY is unset")

        keypair = Keypair.from_base58_string(wallet["private_key"])
        public_key = str(keypair.pubkey())
        if wallet.get("public_key") and wallet["public_key"] != public_key:
            raise AuthError(f"wallet {wallet['id']} public key does not match its private key")

        nonce_result = transport.call_function("getSolanaNonce", {"publicKey": public_key})
        nonce = nonce_result.get("nonce")
        if not nonce:
            raise AuthError("getSolanaNonce returned no nonce")

        message = f"Shard Legends Login: {nonce}"
        signature = base58.b58encode(bytes(keypair.sign_message(message.encode()))).decode()
        verified = transport.call_function("verifySolanaLogin", {
            "publicKey": public_key,
            "signature": signature,
            "message": message,
        })
        custom_token = verified.get("token")
        if not custom_token:
            raise AuthError("verifySolanaLogin returned no token")

        exchanged = transport.request(
            "POST",
            f"{IDENTITY_URL}?key={self.config.firebase_api_key}",
            json_body={"token": custom_token, "returnSecureToken": True},
        )
        id_token = exchanged.get("idToken")
        if not id_token:
            raise AuthError("Firebase exchange returned no idToken")

        return Session(
            wallet_id=wallet["id"],
            public_key=public_key,
            nickname=wallet.get("nickname", ""),
            id_token=id_token,
            refresh_token=exchanged.get("refreshToken", ""),
            local_id=_subject_of(id_token),
            expires_at=int(time.time()) + int(exchanged.get("expiresIn", 3600)) - EXPIRY_MARGIN,
            logged_in_at=int(time.time()),
        )


def _subject_of(id_token: str) -> str:
    try:
        part = id_token.split(".")[1]
        padded = part + "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""
    return claims.get("sub") or claims.get("user_id") or ""
