"""Secret-key parsing for wallet import.

Accepts every export format a Solana user is likely to have, and — critically —
resolves the seed-phrase ambiguity rather than guessing at it.

A mnemonic does not identify one account. Phantom and most wallet apps derive at
BIP44 path m/44'/501'/0'/0', while `solana-keygen` uses the raw BIP39 seed. The same
twelve words therefore yield two different public keys. Importing the wrong one gives
a wallet that authenticates fine and then plays an empty account, so both candidates
are surfaced and the caller confirms which address is theirs.
"""
from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import re
import struct
from dataclasses import dataclass

import base58
from solders.keypair import Keypair

PHANTOM_PATH = "m/44'/501'/0'/0'"
HARDENED = 0x80000000


class KeyImportError(ValueError):
    """The supplied text is not a usable Solana secret."""


@dataclass
class Candidate:
    """One account a secret could refer to."""

    public_key: str
    private_key: str
    source: str

    @property
    def label(self) -> str:
        return f"{self.source}: {self.public_key}"


# --- BIP39 / SLIP-0010 ---------------------------------------------------

def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """BIP39 seed derivation. 2048 PBKDF2-HMAC-SHA512 rounds over the phrase."""
    normalized = " ".join(mnemonic.lower().split())
    return hashlib.pbkdf2_hmac(
        "sha512",
        normalized.encode("utf-8"),
        ("mnemonic" + passphrase).encode("utf-8"),
        2048,
        dklen=64,
    )


def _slip10_master(seed: bytes) -> tuple[bytes, bytes]:
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def _slip10_child(key: bytes, chain_code: bytes, index: int) -> tuple[bytes, bytes]:
    # ed25519 supports hardened derivation only.
    data = b"\x00" + key + struct.pack(">I", index | HARDENED)
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def derive_slip10(seed: bytes, path: str = PHANTOM_PATH) -> bytes:
    """Return the 32-byte private seed at a hardened BIP32-ed25519 path."""
    key, chain_code = _slip10_master(seed)
    for element in path.split("/")[1:]:
        index = int(element.rstrip("'h"))
        key, chain_code = _slip10_child(key, chain_code, index)
    return key


# --- format detection ----------------------------------------------------

def _from_bytes(raw: bytes, source: str) -> Candidate:
    if len(raw) == 64:
        keypair = Keypair.from_bytes(raw)
    elif len(raw) == 32:
        keypair = Keypair.from_seed(raw)
    else:
        raise KeyImportError(f"expected 32 or 64 key bytes, got {len(raw)}")
    return Candidate(public_key=str(keypair.pubkey()),
                     private_key=str(keypair), source=source)


def _looks_like_mnemonic(text: str) -> bool:
    words = text.split()
    return len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words)


def parse_secret(text: str, passphrase: str = "") -> list[Candidate]:
    """Turn a pasted secret into the account(s) it could unlock.

    Returns one candidate for unambiguous formats, and two for a seed phrase.
    """
    text = (text or "").strip()
    if not text:
        raise KeyImportError("nothing to import")

    # JSON byte array, as written by solana-keygen and Phantom's export.
    if text.startswith("["):
        try:
            numbers = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KeyImportError(f"malformed JSON key array: {exc}") from exc
        if not all(isinstance(n, int) and 0 <= n <= 255 for n in numbers):
            raise KeyImportError("key array must contain byte values 0-255")
        return [_from_bytes(bytes(numbers), "json array")]

    if _looks_like_mnemonic(text):
        seed = mnemonic_to_seed(text, passphrase)
        phantom = _from_bytes(derive_slip10(seed, PHANTOM_PATH), f"seed phrase {PHANTOM_PATH}")
        bare = _from_bytes(seed[:32], "seed phrase, no derivation path")
        candidates = [phantom]
        if bare.public_key != phantom.public_key:
            candidates.append(bare)
        return candidates

    # Hex, with or without an 0x prefix.
    hex_text = text[2:] if text.lower().startswith("0x") else text
    if len(hex_text) in (64, 128) and re.fullmatch(r"[0-9a-fA-F]+", hex_text):
        try:
            return [_from_bytes(binascii.unhexlify(hex_text), "hex")]
        except binascii.Error as exc:
            raise KeyImportError(f"malformed hex key: {exc}") from exc

    # Base58, the format `str(Keypair)` produces.
    try:
        raw = base58.b58decode(text)
    except ValueError as exc:
        raise KeyImportError(
            "unrecognised format — expected base58, a JSON byte array, hex, "
            "or a 12/24-word seed phrase"
        ) from exc
    return [_from_bytes(raw, "base58")]


def redact(text: str) -> str:
    """Safe-to-log stand-in for a secret."""
    return f"<{len(text.split()) if ' ' in text else len(text)} unit secret, redacted>"
