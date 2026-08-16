"""Solana transfers between the operator's own wallets.

This is the one place in the project that moves real funds, and it is
deliberately narrow: a plain SOL transfer from one wallet in the vault to
others in the same vault. There is no path here to an address the operator did
not already control, and no token or program interaction of any kind.

The game guardrails in `guardrails.py` are unaffected — those govern game
callables, and `signTransaction` stays denied there. This module is a separate
route that only the Telegram send flow and `slcwctl send` can reach.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from curl_cffi import requests as cffi
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

LAMPORTS_PER_SOL = 1_000_000_000

# A bare account needs this much to stay rent-exempt; going below it can make
# the account unusable, so a source wallet is never drained past it.
RENT_EXEMPT_LAMPORTS = 890_880

# Signature fee per transaction, the current fixed cost on mainnet.
BASE_FEE_LAMPORTS = 5_000


class SolanaError(RuntimeError):
    pass


def sol_to_lamports(amount: float) -> int:
    return int(round(amount * LAMPORTS_PER_SOL))


def lamports_to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


@dataclass
class TransferResult:
    wallet_id: str
    public_key: str
    lamports: int
    signature: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.signature) and not self.error


@dataclass
class SolanaClient:
    """Minimal JSON-RPC client: balances, blockhash, and send."""

    endpoint: str
    timeout: float = 30.0
    _session: object | None = field(default=None, init=False, repr=False)

    @property
    def session(self):
        if self._session is None:
            self._session = cffi.Session()
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def _rpc(self, method: str, params: list, attempts: int = 4) -> dict:
        """Call the RPC, retrying rate limits.

        Public endpoints answer 429 readily, and a batch of transfers hits them
        repeatedly, so backing off here is what keeps a send from half-failing.
        """
        last = ""
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))
            try:
                response = self.session.post(
                    self.endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=self.timeout,
                )
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue

            if response.status_code == 429:
                last = "rate limited (429)"
                continue
            if response.status_code != 200:
                last = f"HTTP {response.status_code}"
                continue

            try:
                payload = response.json()
            except Exception:
                last = "unparseable response"
                continue

            if "error" in payload:
                message = str((payload["error"] or {}).get("message", payload["error"]))
                # A rate limit can also arrive inside a 200 body.
                if "rate" in message.lower() or "429" in message:
                    last = message
                    continue
                raise SolanaError(message)
            return payload.get("result")

        raise SolanaError(last or "rpc failed with no diagnostic")

    # --- reads -----------------------------------------------------------
    def balance(self, public_key: str) -> int:
        result = self._rpc("getBalance", [public_key])
        return int((result or {}).get("value", 0))

    def balances(self, public_keys: list[str]) -> dict:
        """Balances for many accounts in one call, chunked to the RPC limit."""
        out: dict = {}
        for start in range(0, len(public_keys), 100):
            chunk = public_keys[start:start + 100]
            result = self._rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])
            for key, account in zip(chunk, (result or {}).get("value") or []):
                out[key] = int((account or {}).get("lamports", 0) or 0)
        return out

    def latest_blockhash(self) -> Hash:
        result = self._rpc("getLatestBlockhash", [{"commitment": "finalized"}])
        value = (result or {}).get("value") or {}
        blockhash = value.get("blockhash")
        if not blockhash:
            raise SolanaError("no blockhash returned")
        return Hash.from_string(blockhash)

    # --- writes ----------------------------------------------------------
    def send_sol(self, sender: Keypair, recipient: str, lamports: int) -> str:
        """Sign and submit one transfer. Returns the signature."""
        if lamports <= 0:
            raise SolanaError("amount must be positive")

        instruction = transfer(TransferParams(
            from_pubkey=sender.pubkey(),
            to_pubkey=Pubkey.from_string(recipient),
            lamports=lamports,
        ))
        message = Message.new_with_blockhash(
            [instruction], sender.pubkey(), self.latest_blockhash())
        transaction = Transaction([sender], message, message.recent_blockhash)

        signature = self._rpc("sendTransaction", [
            bytes(transaction).hex(),
            {"encoding": "hex", "skipPreflight": False,
             "preflightCommitment": "confirmed", "maxRetries": 3},
        ])
        if not signature:
            raise SolanaError("send returned no signature")
        return str(signature)


@dataclass
class DistributionPlan:
    """A proposed fan-out, priced before anything is signed."""

    source_id: str
    source_public_key: str
    per_recipient_lamports: int
    recipients: list = field(default_factory=list)
    source_balance: int = 0

    @property
    def count(self) -> int:
        return len(self.recipients)

    @property
    def total_lamports(self) -> int:
        return self.per_recipient_lamports * self.count

    @property
    def fee_lamports(self) -> int:
        return BASE_FEE_LAMPORTS * self.count

    @property
    def required_lamports(self) -> int:
        return self.total_lamports + self.fee_lamports + RENT_EXEMPT_LAMPORTS

    @property
    def affordable(self) -> bool:
        return self.source_balance >= self.required_lamports

    @property
    def shortfall_lamports(self) -> int:
        return max(0, self.required_lamports - self.source_balance)


def plan_distribution(source: dict, wallets: list[dict], amount_sol: float,
                      source_balance: int) -> DistributionPlan:
    """Work out who receives what, without touching the network.

    The source is excluded from its own distribution, and the plan is checked
    against the balance before any signing happens.
    """
    per_recipient = sol_to_lamports(amount_sol)
    recipients = [w for w in wallets if w["public_key"] != source["public_key"]]
    return DistributionPlan(
        source_id=source["id"],
        source_public_key=source["public_key"],
        per_recipient_lamports=per_recipient,
        recipients=recipients,
        source_balance=source_balance,
    )


@dataclass
class SweepPlan:
    """Send everything spare from many wallets back to one destination."""

    destination_id: str
    destination_public_key: str
    # (wallet, lamports_to_send) for each wallet with something worth moving.
    entries: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def total_lamports(self) -> int:
        return sum(amount for _, amount in self.entries)


def sweepable_lamports(balance: int) -> int:
    """How much a wallet can send while staying alive.

    The rent-exempt minimum has to stay behind, and the transaction itself
    costs a signature fee, so a wallet holding only dust yields nothing.
    """
    return max(0, balance - RENT_EXEMPT_LAMPORTS - BASE_FEE_LAMPORTS)


def plan_sweep(wallets: list[dict], destination: dict, balances: dict) -> SweepPlan:
    """Work out what each wallet would return to the destination."""
    plan = SweepPlan(destination_id=destination["id"],
                     destination_public_key=destination["public_key"])
    for wallet in wallets:
        if wallet["public_key"] == destination["public_key"]:
            continue
        amount = sweepable_lamports(balances.get(wallet["public_key"], 0))
        if amount > 0:
            plan.entries.append((wallet, amount))
        else:
            plan.skipped.append(wallet)
    return plan


def execute_sweep(client: SolanaClient, plan: SweepPlan) -> list[TransferResult]:
    """Return funds from every wallet in the plan, reporting each separately."""
    results = []
    for wallet, amount in plan.entries:
        try:
            keypair = Keypair.from_base58_string(wallet["private_key"])
            signature = client.send_sol(
                keypair, plan.destination_public_key, amount)
            results.append(TransferResult(
                wallet["id"], wallet["public_key"], amount, signature=signature))
        except Exception as exc:
            results.append(TransferResult(
                wallet["id"], wallet["public_key"], amount,
                error=f"{type(exc).__name__}: {exc}"))
    return results


def parse_amount(text: str) -> float:
    """Read a hand-typed SOL amount.

    Accepts a comma decimal separator, since that is what an Indonesian keyboard
    produces and silently misreading it would move the wrong sum by a factor of
    a thousand.
    """
    cleaned = (text or "").strip().replace(",", ".").replace("SOL", "").replace("sol", "")
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("no amount given")
    try:
        amount = float(cleaned)
    except ValueError:
        raise ValueError(f"{text!r} is not a number") from None
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    if amount > 1000:
        raise ValueError("amount above 1000 SOL looks like a mistake")
    return amount


def execute_distribution(client: SolanaClient, source: dict,
                         plan: DistributionPlan) -> list[TransferResult]:
    """Send to every recipient, reporting each outcome separately.

    One failed transfer does not abort the rest: partial success is reported
    honestly so the operator can retry only what did not land.
    """
    if not plan.affordable:
        raise SolanaError(
            f"balance short by {lamports_to_sol(plan.shortfall_lamports):.6f} SOL")

    keypair = Keypair.from_base58_string(source["private_key"])
    results = []
    for wallet in plan.recipients:
        try:
            signature = client.send_sol(
                keypair, wallet["public_key"], plan.per_recipient_lamports)
            results.append(TransferResult(
                wallet["id"], wallet["public_key"],
                plan.per_recipient_lamports, signature=signature))
        except Exception as exc:
            results.append(TransferResult(
                wallet["id"], wallet["public_key"],
                plan.per_recipient_lamports, error=f"{type(exc).__name__}: {exc}"))
    return results
