"""Hard allowlist enforced before any request is constructed.

This is deliberately not configurable. A denied callable cannot be enabled by an
environment variable, a Telegram button, or a code path that "knows better" — the
check runs inside the transport layer, so every route to the network passes it.
"""
from __future__ import annotations


class GuardrailViolation(RuntimeError):
    """Raised when code attempts a callable outside the allowlist."""


# Callables the orchestrator may invoke. Anything absent is denied.
ALLOWED_CALLABLES = frozenset({
    # auth
    "getSolanaNonce",
    "verifySolanaLogin",
    # onboarding (idempotent, no cost)
    "initializeImperialStats",
    "initializeNewInventory",
    "migrateToNewInventory",
    "startFirstTravel",
    "updateDisplayName",
    "completeNewbieQuest",
    # free claims
    "claimInitialReward",
    "finishActivity",
    # gameplay, energy- or gold-funded only
    "startRelax",
    "startProduction",
    "startFarming",
    "startTravel",
    "startBattle",
    "processTurn",
    "openChests",
    "equipItem",
    "spendAttributePoints",
})

# Denied permanently. Listed explicitly so the reason is greppable and so a typo in
# ALLOWED_CALLABLES can never silently re-enable one of these.
DENIED_CALLABLES = frozenset({
    "spendDiamonds",
    "purchaseDiamonds",
    "buyDiamonds",
    "instantComplete",
    "speedUp",
    "speedUpActivity",
    # Confirmed from the frontend bundle: charges 5 * ceil(seconds_left / 60)
    # diamonds and reports the spend as `deductedDiamonds`.
    "skipActivityTime",
    "instantHeal",
    # Charges 50 or 1000 gold depending on the city's accessType. Left denied
    # until the return on entering a paid city is measured.
    "payCityEntryFee",
    # Farming in "diamond" mode buys a license for 49 * 3^(tier-1) diamonds.
    "purchaseFarmingLicense",
    # Arena has no measured reward or loss model yet.
    "joinArenaQueue",
    "leaveArenaQueue",
    # Grants a level from a card draw; the cost model is not established.
    "buyLevel",
    "withdraw",
    "requestWithdrawal",
    "claimWithdrawalReward",
    "transfer",
    "transferTokens",
    "sendTransaction",
    "signTransaction",
    "createOrder",
    "createMarketOrder",
    "cancelOrder",
    "buyOrder",
})


def check(name: str) -> None:
    """Raise GuardrailViolation unless `name` is explicitly allowed."""
    if name in DENIED_CALLABLES:
        raise GuardrailViolation(
            f"{name!r} is permanently denied: spends premium currency, moves funds, "
            "or signs a transaction"
        )
    if name not in ALLOWED_CALLABLES:
        raise GuardrailViolation(
            f"{name!r} is not on the allowlist. Add it to ALLOWED_CALLABLES only after "
            "confirming it costs no diamonds and moves no funds."
        )


def is_allowed(name: str) -> bool:
    try:
        check(name)
    except GuardrailViolation:
        return False
    return True
