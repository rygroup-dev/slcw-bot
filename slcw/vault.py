"""Encrypted wallet storage.

Private keys previously sat in plaintext at data/wallets.json. They now live in
data/wallets.enc under AES-256-GCM with a scrypt-derived key. The passphrase is
supplied at runtime and held in process memory only — it is never written to disk,
never logged, and never sent to Telegram in an outbound message.
"""
from __future__ import annotations

import base64
import json
import os
import random
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

from .config import DATA
from .transport import build_persona

VAULT_PATH = DATA / "wallets.enc"
LEGACY_PATH = DATA / "wallets.json"

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32

ADJECTIVES = ("Ashen", "Iron", "Silent", "Storm", "Raven", "Ember", "Frost", "Violet",
              "Amber", "Hollow", "Bright", "Quiet", "Rust", "Pale", "Wilder", "Dusk")
NOUNS = ("Vale", "Forge", "Crown", "Warden", "Harbor", "Spire", "Trail", "Keeper",
         "Hollow", "March", "Gate", "Reach", "Fen", "Ridge", "Hearth", "Ward")


class VaultLocked(RuntimeError):
    """Raised when wallet data is requested before the vault is unlocked."""


class VaultError(RuntimeError):
    """Wrong passphrase or corrupt vault file."""


def _derive(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_LENGTH, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


@dataclass
class Vault:
    """Holds decrypted wallets in memory for the lifetime of the process."""

    _wallets: list[dict] | None = None
    _passphrase: str | None = None

    @property
    def is_unlocked(self) -> bool:
        return self._wallets is not None

    @property
    def exists(self) -> bool:
        return VAULT_PATH.exists()

    def unlock(self, passphrase: str) -> int:
        """Decrypt the vault. Returns the wallet count."""
        if not VAULT_PATH.exists():
            # First run: an empty vault is created on the first write.
            self._wallets = []
            self._passphrase = passphrase
            return 0
        try:
            blob = json.loads(VAULT_PATH.read_text())
            salt = base64.b64decode(blob["salt"])
            nonce = base64.b64decode(blob["nonce"])
            ciphertext = base64.b64decode(blob["ciphertext"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise VaultError(f"vault file is unreadable: {exc}") from exc

        try:
            plaintext = AESGCM(_derive(passphrase, salt)).decrypt(nonce, ciphertext, None)
        except Exception as exc:  # InvalidTag and friends
            raise VaultError("wrong passphrase or corrupted vault") from exc

        self._wallets = json.loads(plaintext.decode("utf-8"))
        self._passphrase = passphrase
        return len(self._wallets)

    def lock(self) -> None:
        self._wallets = None
        self._passphrase = None

    def _require(self) -> list[dict]:
        if self._wallets is None:
            raise VaultLocked("vault is locked — unlock it before reading wallets")
        return self._wallets

    def _persist(self) -> None:
        if self._passphrase is None:
            raise VaultLocked("cannot persist while locked")
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = _derive(self._passphrase, salt)
        ciphertext = AESGCM(key).encrypt(
            nonce, json.dumps(self._require()).encode("utf-8"), None)
        _write_private(VAULT_PATH, json.dumps({
            "version": 1,
            "kdf": "scrypt",
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }, indent=2))

    # --- reads -----------------------------------------------------------
    def wallets(self) -> list[dict]:
        return list(self._require())

    def get(self, wallet_id: str) -> dict | None:
        return next((w for w in self._require() if w["id"] == wallet_id), None)

    def public_summary(self) -> list[dict]:
        """Wallet listing safe to render anywhere — no private material."""
        return [{
            "id": w["id"],
            "nickname": w.get("nickname", ""),
            "public_key": w.get("public_key", ""),
            "enabled": w.get("enabled", True),
            "proxy": "set" if w.get("proxy") else "none",
        } for w in self._require()]

    # --- writes ----------------------------------------------------------
    def create_wallets(self, count: int, prefix: str = "wallet") -> list[dict]:
        """Generate fresh keypairs. Never broadcasts anything or moves funds."""
        wallets = self._require()
        used = {w.get("nickname") for w in wallets}
        created = []
        for _ in range(count):
            keypair = Keypair()
            nickname = _unique_nickname(used)
            used.add(nickname)
            wallet_id = f"{prefix}-{len(wallets) + 1:02d}"
            record = {
                "id": wallet_id,
                "nickname": nickname,
                "public_key": str(keypair.pubkey()),
                "private_key": str(keypair),
                "persona": build_persona(wallet_id),
                "proxy": None,
                "enabled": True,
                "onboarded": False,
                # Each wallet sleeps on its own schedule so daily activity patterns
                # differ between accounts instead of moving in lockstep.
                "sleep_anchor_hour": random.randint(0, 23),
                "sleep_hours": round(random.uniform(6.0, 9.0), 2),
            }
            wallets.append(record)
            created.append(record)
        self._persist()
        return created

    def import_wallet(self, private_key: str, public_key: str,
                      nickname: str | None = None, prefix: str = "wallet") -> dict:
        """Add an existing account to the vault.

        The keypair is re-derived from the private key and checked against the
        supplied public key, so a truncated or mistyped paste fails here rather
        than surfacing later as an account that authenticates into the wrong game
        profile.
        """
        derived = str(Keypair.from_base58_string(private_key).pubkey())
        if derived != public_key:
            raise ValueError(
                f"private key derives {derived}, not the expected {public_key}")

        wallets = self._require()
        existing = next((w for w in wallets if w.get("public_key") == derived), None)
        if existing is not None:
            raise ValueError(f"{derived} is already in the vault as {existing['id']}")

        used = {w.get("nickname") for w in wallets}
        wallet_id = f"{prefix}-{len(wallets) + 1:02d}"
        while any(w["id"] == wallet_id for w in wallets):
            wallet_id = f"{prefix}-{len(wallets) + 1 + len(used):02d}"

        record = {
            "id": wallet_id,
            "nickname": nickname or _unique_nickname(used),
            "public_key": derived,
            "private_key": private_key,
            "persona": build_persona(wallet_id),
            "proxy": None,
            "enabled": True,
            # Imported accounts already exist in-game, so onboarding is skipped.
            "onboarded": True,
            "imported": True,
            "sleep_anchor_hour": random.randint(0, 23),
            "sleep_hours": round(random.uniform(6.0, 9.0), 2),
        }
        wallets.append(record)
        self._persist()
        return record

    def update(self, wallet_id: str, **changes) -> dict:
        wallet = self.get(wallet_id)
        if wallet is None:
            raise KeyError(wallet_id)
        wallet.update(changes)
        self._persist()
        return wallet

    def import_legacy(self, passphrase: str) -> int:
        """Encrypt a plaintext data/wallets.json, then remove the plaintext file."""
        if not LEGACY_PATH.exists():
            return 0
        legacy = json.loads(LEGACY_PATH.read_text())
        if self._wallets is None:
            self._wallets = []
            self._passphrase = passphrase
        known = {w["public_key"] for w in self._wallets}
        added = 0
        for entry in legacy:
            if entry.get("public_key") in known:
                continue
            wallet_id = entry.get("id") or f"wallet-{len(self._wallets) + 1:02d}"
            self._wallets.append({
                "id": wallet_id,
                "nickname": entry.get("nickname", ""),
                "public_key": entry["public_key"],
                "private_key": entry["private_key"],
                "persona": build_persona(wallet_id),
                "proxy": None,
                "enabled": True,
                "onboarded": True,
                "sleep_anchor_hour": random.randint(0, 23),
                "sleep_hours": round(random.uniform(6.0, 9.0), 2),
            })
            added += 1
        self._persist()
        LEGACY_PATH.replace(LEGACY_PATH.with_suffix(".json.migrated"))
        return added


def _unique_nickname(used: set) -> str:
    for _ in range(200):
        candidate = f"{random.choice(ADJECTIVES)}{random.choice(NOUNS)}{random.randint(10, 99)}"
        if candidate not in used:
            return candidate
    return f"Wanderer{secrets.randbelow(10**6):06d}"
