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
    # Levelling is a manual click in the profile, and only the diamond booster
    # costs anything: the client charges 99*(level+1) diamonds in that branch
    # alone. slcw.leveling pins the booster to "none", so this spends XP only.
    "buyLevel",
    "claimInitialReward",
    "finishActivity",
    "completeNewbieQuest",
    # Three per day, no cost. Energy is the binding constraint on almost every
    # action, so this is among the most valuable calls in the game.
    "refillEnergyFree",

    # --- selling gear back, gold in and a slot freed --------------------
    # Measured live on 2026-08-22 before allowlisting, which is what this list
    # asks for: one plate_greaves_t2 paid 8,948 gold with taxAmount 0, the
    # premium balance stayed at 0.0 across the call, and it worked from farm_3
    # with no travel to a city. Gold in, one slot freed, nothing spent.
    #
    # It is not createMarketOrder, which stays denied below: that one is a
    # trade with other players and the client checks premium_balance >= 1
    # before it will even submit. This sells to the shop at a server-set price.
    "sellEquipmentItem",

    # --- quests and tasks, free to run and to claim ---------------------
    "getTaskStatus",
    "acceptTask",
    "claimTaskReward",
    "startTaskBattle",
    # Present in the bundle but confirmed 404 live (2026-08-17): no deployed
    # Cloud Function answers this name, so it cannot be wired up as-is.
    # Left allowlisted only in case a future build ships it.
    "generateMiningQuests",
    "completeMiningQuest",
    # generateCitizenshipQuests/completeCitizenshipQuest are real endpoints,
    # but confirmed live (2026-08-17) to be unreachable without spending
    # diamonds — see becomeCitizen below. Left allowlisted for completeness;
    # they will always fail with FAILED_PRECONDITION ("not a citizen of any
    # city") under this bot's no-diamonds policy, same as any other denied
    # premium path.
    "generateCitizenshipQuests",

    # --- clans, read and free participation -----------------------------
    # Reverse-engineered live 2026-08-21. These four cost no premium currency
    # and move no funds: two are reads, one applies to join, one withdraws that
    # application again.
    "searchClans",
    "getClanMembers",
    "applyClan",
    "cancelApplication",
    "leaveClan",
    # Spends raw drops that the market has no bids for at all, in exchange for
    # DKP and clan XP. The only clan action that costs the fleet nothing it
    # could otherwise have sold.
    "submitQuestResources",

    # --- operator-only: allowed to execute, never offered as a candidate ---
    # These three run a clan the operator owns. They are reachable exclusively
    # through explicit slcwctl commands; build_candidates never emits them, so
    # no unattended wallet can decide to take them on its own.
    #
    # createClan spends 20,000 gold in one call. That is not premium currency
    # and it is not someone else's money, so denying it outright was wrong —
    # but it is far too large a commitment for a decision loop, hence the
    # operator gate rather than the allowlist alone.
    "createClan",
    # Accepts or rejects a join request to the operator's own clan.
    "resolveApplication",
    # Starts the clan's one free weekly quest. In a clan of the operator's own
    # wallets, spending that 7-day cooldown is their call to make.
    "generateClanQuest",
    # Moves gold out of the wallet into a treasury this operator may not
    # control, so it is allowlisted but gated behind SLCW_CLAN_DONATE_GOLD,
    # which is off by default. See slcw/clan.py.
    "makeDonation",
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
    # --- clan operations this bot must never take -------------------------
    # Measured live 2026-08-21. Each of these either spends premium currency,
    # spends a large gold sum, or takes an irreversible decision on behalf of a
    # whole clan of other people. An unattended fleet has no business doing any
    # of them, so they are denied by construction rather than left to a flag.
    "extendClanQuest": "costs 1,399 diamonds or 2,599 $SLCW to add 24h",
    "disbandClan": "irreversibly destroys a clan and its treasury",
    "distributeTreasury": "moves the whole clan treasury to other players",
    "transferLeadership": "hands the clan to another account, 72h and final",
    "kickMember": "removes another player from their clan",
    "setMemberRole": "changes another player's standing in the clan",
    "updateClanSettings": "rewrites a clan's public identity",

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
    # Confirmed live (2026-08-17): a wallet holding 9,445 gold and 0 diamonds
    # was rejected with "Insufficient diamonds" on the cheapest of 3 tiers —
    # so despite the bundle's field being named plain `cost` (399/2199/7799
    # for citizen/entrepreneur/aristocrat), citizenship is diamond-gated, not
    # gold-gated. That also keeps generateCitizenshipQuests/
    # completeCitizenshipQuest unreachable under this bot's no-diamonds
    # policy — see the comment there.
    "becomeCitizen": "confirmed live: diamond-gated (\"Insufficient diamonds\"), not gold",
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
