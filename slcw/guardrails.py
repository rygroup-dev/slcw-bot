"""Hard allowlist enforced before any request is constructed.

This is deliberately not configurable. A denied callable cannot be enabled by an
environment variable, a Telegram button, or a code path that "knows better" — the
check runs inside the transport layer, so every route to the network passes it.

The classification below covers every callable found in the game's frontend bundle.
Anything that spends premium currency, moves funds, or has no measured return stays
denied even though we know it exists and how to call it.
"""
from __future__ import annotations


class GuardrailViolation(RuntimeError):
    """Raised when code attempts a callable outside the allowlist."""


# Callables the orchestrator may invoke. Everything here is free, or paid for with
# energy, gold, or materials the account already earned.
ALLOWED_CALLABLES = frozenset({
    # --- authentication -------------------------------------------------
    "getSolanaNonce",
    "verifySolanaLogin",

    # --- onboarding, idempotent and free --------------------------------
    "initializeImperialStats",
    "initializeNewInventory",
    "migrateToNewInventory",
    "startFirstTravel",
    "updateDisplayName",

    # --- free claims ----------------------------------------------------
    "claimInitialReward",
    "finishActivity",
    "completeNewbieQuest",
    # Three per day, no cost. Energy is the binding constraint on almost every
    # action, so this is among the most valuable calls in the game.
    "refillEnergyFree",

    # --- quests and tasks, free to run and to claim ---------------------
    "getTaskStatus",
    "acceptTask",
    "claimTaskReward",
    "startTaskBattle",
    "generateMiningQuests",
    "completeMiningQuest",
    "generateCitizenshipQuests",
    "completeCitizenshipQuest",

    # --- activities funded by energy, gold, or materials ----------------
    "startRelax",
    "startProduction",
    "startFarming",
    "startRefining",
    "startCrafting",
    "startHunting",
    "startTravel",
    "startBattle",
    "processTurn",

    # --- inventory and equipment, free ----------------------------------
    "openChests",
    "equipItem",
    "unequipItem",
    "sortInventory",
    "spendAttributePoints",

    # --- crafting shop, gold-priced and required by refining ------------
    # Catalysts are not traded on the market; they are the one input that must be
    # bought. Price is fixed per tier, so the spend is fully predictable.
    "purchaseCraftingItem",
})

# Denied permanently, with the reason recorded. Listed explicitly so a typo in
# ALLOWED_CALLABLES can never silently re-enable one of these.
DENIED_CALLABLES = {
    # --- premium currency ------------------------------------------------
    "spendDiamonds": "spends diamonds",
    "purchaseDiamonds": "buys diamonds with real money",
    "buyDiamonds": "buys diamonds with real money",
    # Confirmed from the bundle: charges 5 * ceil(seconds_left / 60) diamonds and
    # reports the spend back as `deductedDiamonds`.
    "skipActivityTime": "spends diamonds to skip a timer",
    "instantComplete": "spends diamonds to skip a timer",
    "speedUp": "spends diamonds to skip a timer",
    "speedUpActivity": "spends diamonds to skip a timer",
    "instantHeal": "spends diamonds",
    # Cost doubles per use: 99 * 2^(refills today) diamonds.
    "refillEnergyPaid": "spends diamonds for energy the free call also provides",
    "instantCompleteMiningQuest": "spends diamonds",
    "premiumSearchMount": "spends diamonds",
    "purchaseImperialSeal": "spends diamonds",
    "purchaseInitialRewardsPass": "spends diamonds",
    "purchaseMiningGoldSlot": "spends diamonds",
    "purchaseStarterPack": "real-money purchase",
    "buyBiomantPass": "premium purchase",

    # --- funds and transactions -----------------------------------------
    "withdraw": "moves funds",
    "requestWithdrawal": "moves funds",
    "claimWithdrawalReward": "moves funds",
    "transfer": "moves funds",
    "transferTokens": "moves funds",
    "sendTransaction": "signs a transaction",
    "signTransaction": "signs a transaction",
    "linkSolanaWallet": "rebinds the account's wallet",

    # --- markets: surfaced for the operator, never traded automatically --
    "createOrder": "places a market order",
    "createMarketOrder": "places a market order",
    "cancelOrder": "cancels a market order",
    "buyOrder": "fills a market order",
    "placeGoldOrder": "places a gold-market order",
    "cancelGoldOrder": "cancels a gold-market order",
    "executeGoldMarketOrder": "fills a gold-market order",

    # --- gold-costed, with no measured return ----------------------------
    "payCityEntryFee": "costs 50 or 1000 gold with no measured return",
    "buyLevel": "cost model not established",
    "becomeCitizen": "costs gold, return not measured",
    "renounceCitizenship": "discards a paid-for status",
    "expandInventory": "costs currency, return not measured",
    "resetPoints": "costs currency to undo attribute spending",
    "upgradeMiningQuests": "costs currency, return not measured",
    "upgradeMounts": "costs currency, return not measured",
    "upgradeBiomantTalent": "costs currency, return not measured",
    "restoreMountStamina": "costs currency, return not measured",
    "equipMount": "mount system not modelled",
    "searchMountActivity": "mount system not modelled",

    # --- consumes materials irreversibly, no model ------------------------
    "sharpenItem": "consumes materials, outcome not modelled",
    "awakenItem": "consumes materials, outcome not modelled",
    "startSoulExtraction": "consumes items irreversibly",
    "evolveGrade": "consumes imperial seals irreversibly",
    "deleteInventoryItem": "destroys items",

    # --- risk or loss not modelled ----------------------------------------
    "joinArenaQueue": "no reward or loss model",
    "leaveArenaQueue": "no reward or loss model",
    "dispatchCaravan": "can be robbed; loss model not established",
    "startExpedition": "reward and cost model not measured",
    "finishExpedition": "reward and cost model not measured",
    "claimExpeditionRewards": "reward and cost model not measured",
    "handleReferral": "binds accounts together; operator decision",
}


def check(name: str) -> None:
    """Raise GuardrailViolation unless `name` is explicitly allowed."""
    reason = DENIED_CALLABLES.get(name)
    if reason is not None:
        raise GuardrailViolation(f"{name!r} is permanently denied: {reason}")
    if name not in ALLOWED_CALLABLES:
        raise GuardrailViolation(
            f"{name!r} is not on the allowlist. Add it to ALLOWED_CALLABLES only "
            "after confirming it costs no premium currency and moves no funds."
        )


def is_allowed(name: str) -> bool:
    try:
        check(name)
    except GuardrailViolation:
        return False
    return True


def denial_reason(name: str) -> str:
    return DENIED_CALLABLES.get(name, "not on the allowlist")
