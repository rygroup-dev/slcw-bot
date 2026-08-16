"""Wallet export.

Everywhere else in this project, private keys stay inside the vault and never
reach a message, a log, or the ledger. Export is the deliberate exception: a
vault that cannot be backed up is one VPS failure away from losing every
account, which is its own kind of loss.

The payload is written so it can be read straight back by `slcwctl import` or
any Solana wallet, and it carries a warning header so a file found later is not
mistaken for something harmless.
"""
from __future__ import annotations

import datetime as _dt
import json

EXPORT_VERSION = 1

WARNING = ("PRIVATE KEYS IN PLAIN TEXT. Anyone holding this file controls these "
           "wallets completely. Store it encrypted and delete any copy you do "
           "not need.")


def export_payload(wallets: list[dict], note: str = "") -> dict:
    """Build the export document.

    Only the fields needed to restore an account are included. Operational
    state — browser persona, sleep schedule, proxy — is deliberately left out:
    it is regenerated on import and would only make the file harder to read.
    """
    return {
        "version": EXPORT_VERSION,
        "warning": WARNING,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "wallet_count": len(wallets),
        "note": note,
        "wallets": [
            {
                "id": w.get("id", ""),
                "nickname": w.get("nickname", ""),
                "public_key": w.get("public_key", ""),
                "private_key": w.get("private_key", ""),
            }
            for w in wallets
        ],
    }


def export_json(wallets: list[dict], note: str = "") -> str:
    return json.dumps(export_payload(wallets, note), indent=2)


def export_filename(count: int, now: _dt.datetime | None = None) -> str:
    stamp = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"slcw-wallets-{count}-{stamp}.json"


def verify_payload(payload: dict) -> list[str]:
    """Check an export round-trips before it is trusted as a backup.

    A backup nobody validated is a backup nobody has, so every key is
    re-derived and checked against the public key stored beside it.
    """
    from solders.keypair import Keypair

    problems = []
    wallets = payload.get("wallets") or []
    if not wallets:
        return ["export contains no wallets"]

    for entry in wallets:
        wallet_id = entry.get("id", "?")
        secret = entry.get("private_key") or ""
        expected = entry.get("public_key") or ""
        if not secret or not expected:
            problems.append(f"{wallet_id}: missing key material")
            continue
        try:
            derived = str(Keypair.from_base58_string(secret).pubkey())
        except Exception as exc:
            problems.append(f"{wallet_id}: unreadable private key ({exc})")
            continue
        if derived != expected:
            problems.append(f"{wallet_id}: key derives {derived}, not {expected}")
    return problems
