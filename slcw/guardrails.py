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

    # --- raising the grade, which is what lifts the level ceiling --------
    # A character cannot pass level 15 x grade, so a grade-1 wallet at level 15
    # discards every point of XP it earns. All three of these were denied until
    # 2026-08-22, two of them on wrong information:
    #
    #   purchaseImperialSeal was listed as spending diamonds. It does not: the
    #   imperial shop checks `balance` against a gold price (base 3,500, moved
    #   by the city warehouse; Greyholm quoted 2,883 when measured).
    #
    #   payCityEntryFee was listed as having no measured return. Greyholm is
    #   `accessType: "paid_50"`, so it costs fifty gold, and what it buys is
    #   the shop and the altar behind it.
    #
    #   evolveGrade does spend seals irreversibly, and that has not changed.
    #   It is allowed on an explicit operator decision, and the decision loop
    #   only reaches it when the level gate is already met and the seals are
    #   already in hand — it never buys its way toward a grade it cannot take.
    "purchaseImperialSeal",
    "payCityEntryFee",
    "evolveGrade",

    # --- selling what the workshops make, which is where the gold is ----
    # Measured before allowlisting: five copper_ingot returned
    # {totalFilled: 5, totalGold: 4495, tax: 899} and the balance rose by
    # 3,596. Paid in gold, no premium currency, no travel.
    #
    # This is not createMarketOrder, which stays denied: that is the player
    # market, it wants premium to place an order at all, and pulling every open
    # order found 3,495 of them across three distinct items — none of which the
    # fleet holds. The Black Market had 6,000, and its buy side is refined
    # goods with thousands of units of standing demand.
    "executeBlackMarketOrder",

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

    # --- destroys items, deliberately, under a proof ---------------------
    # The only irreversible thing the bot does to an item, opened on the
    # operator's decision after 24 of 30 wallets jammed at 40/40 slots with no
    # way out: monster drops carry no bid, feed no recipe, refine into nothing,
    # the gear shop's stock is full, and expandInventory is priced in diamonds
    # no wallet has. A full bag then refuses openChests, claimInitialReward and
    # upgradeEquip, so the fighting that fills it stops paying.
    #
    # Signature is deleteInventoryItem({slotIndex}) — a slot, not a template.
    # What may be destroyed is decided in slcw/discard.py against five gates and
    # a fresh market, never here, and the whole branch is off unless
    # SLCW_DISCARD_JUNK says otherwise.
    "deleteInventoryItem",

    # --- free bookkeeping ------------------------------------------------
    # Recomputes the referral tree's cumulative levels and pays gold for the
    # difference; the reply carries `goldRewarded`. Costs nothing and binds
    # nothing — handleReferral is the call that binds accounts, and that stays
    # denied.
    "recalculateReferralLevels",
    # Free, and measured on a wallet sitting at 40 of 40: it frees nothing.
    # Slots are not stacks — a bag of 31 distinct items filled 40 slots because
    # every equipment instance occupies its own. Allowed because it costs
    # nothing, wired to nothing because it achieves nothing.
    "sortInventory",

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
    # Caravan trade, measured live 2026-08-23 and again after the first model
    # of it turned out to be wrong. It buys a warehouse's output with gold —
    # the cargo never touches the bag — hauls it at twenty seconds per unit of
    # map distance, and sells it on arrival through finishActivity.
    #
    # A workshop prices its shelf: ceil(base * (2 - held/half)) while held is
    # under half of warehouseCapacity, sliding to half base when it is full. A
    # whole load is priced once, off the shelf as it stands. Virtan, the one
    # trade hub, is not a warehouse at all but a market maker on a flat 20%
    # spread: it sells at 1.2x base and buys at 0.8x, whatever it is holding,
    # and takes no tax on what it buys. Reading its ask off the price curve
    # instead overstated every route out of it fivefold, which is why the
    # numbers here are measured rather than derived.
    #
    # The server also decides the roads: "Caravans from cities must go to Hub".
    # From an ordinary city the only legal destination is Virtan; from Virtan,
    # any city that consumes the good. City to city is refused outright.
    #
    # Two clean runs, neither robbed: ten chronicle_page Ostrim -> Virtan cost
    # 27,890 and returned 33,600; ten battle_ember Virtan -> Greyholm cost
    # 54,000 and returned 59,599. Call it 5k-11k profit for twenty energy —
    # several times what the same energy earns in the arena, and the reason
    # slcw/caravan.py prices every leg against the live `cities` documents
    # before the call rather than trusting a route table.
    #
    # Robbery is the one thing still unmeasured: cities carry a
    # `caravanRobberyDefenseBonus`, so it exists, but no chance and no loss
    # appear anywhere in the client — it is resolved server-side. It is
    # allowlisted anyway because a lost load costs the load, which the wallet
    # already committed knowingly, and both measured runs arrived intact.
    "dispatchCaravan",
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
    "renounceCitizenship": "discards a paid-for status",
    # Read straight off the inventory page: the cost is
    # 100 * 2^inventoryExpansions paid from premium_balance, for +10 slots.
    # Reachable in principle — the gold market bids diamonds for gold, and on
    # 2026-08-22 the best bid was 25 diamonds per lot of 100,000 gold, so the
    # first expansion costs one wallet 400,000 gold. It stays denied because
    # the new slots would hold the same items the discard policy destroys for
    # having no bid, no recipe and no refine: 400,000 gold to postpone the
    # treadmill by ten slots buys nothing.
    "expandInventory": "diamond-priced; the slots would only hold junk",
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

    # --- risk or loss not modelled ----------------------------------------
    "joinArenaQueue": "no reward or loss model",
    "leaveArenaQueue": "no reward or loss model",
    "startExpedition": "reward and cost model not measured",
    "finishExpedition": "reward and cost model not measured",
    "claimExpeditionRewards": "reward and cost model not measured",
    "handleReferral": "binds accounts together; operator decision",

    # --- classified 2026-08-22, after re-reading every route in the app ---
    # The bundle sweep found 90 callables; these are the ones nothing had ever
    # decided about. None is a gap in what the bot can do — each is a
    # deliberate no.
    "buyEngineerPass": "premium purchase",
    "buyMerchantPass": "premium purchase",
    "buyStrategistPass": "premium purchase",
    "upgradeEngineerTalent": "costs currency, return not measured",
    "upgradeMerchantTalent": "costs currency, return not measured",
    "upgradeStrategistTalent": "costs currency, return not measured",
    # The shop sells and buys the same gear, and it buys lower than it sells.
    "buyEquipmentItem": "spends gold into a spread that runs against us",
    # Limit orders park goods in a book nothing here watches; the bot fills at
    # market instead, which is measured and settles in one call.
    "placeBlackMarketLimitOrder": "parks goods in an order book nothing watches",
    "cancelBlackMarketOrder": "only needed for limit orders, which are denied",
    "upgradeBlackMarketSlots": "buys limit-order slots the bot does not use",
    # The player market: 3,495 open orders across three distinct items on
    # 2026-08-22, none of them anything the fleet holds, and createMarketOrder
    # needs premium to place at all. Nothing to fill.
    "fulfillMarketOrder": "player market has no bids on anything the fleet holds",
    "cancelMarketOrder": "the bot places no player-market orders to cancel",
    "createAuction": "auction outcome and timing not modelled",
    "placeAuctionBid": "auction outcome and timing not modelled",
    "claimAuctionItem": "only reachable through auctions, which are denied",
    "resolveAuctionSeller": "only reachable through auctions, which are denied",
    "createWithdrawalRequest": "moves funds off the account",
    "recalculateWithdrawalStats": "withdrawal bookkeeping; nothing to recalculate",
    "submitPvPMove": "arena has no reward or loss model",
    "sendToChat": "posts publicly under the account's name",
    "linkDiscordAccount": "binds the account to an external identity",
    "unlinkDiscordAccount": "binds the account to an external identity",
    "syncDiscordRoles": "binds the account to an external identity",
    "linkTelegramAccount": "binds the account to an external identity",
    "unlinkTelegramAccount": "binds the account to an external identity",

    # --- classified 2026-08-22, from a re-fetch of the live bundle ---------
    # Three route chunks changed since the 2026-08-16 capture: /clan and
    # /wildland only in ways the bot does not touch (clan search, a favourite-
    # monsters list kept in the browser's own storage), and /vanguard, which is
    # new machinery: a wave dungeon at city_11, turn-based, where both sides
    # pick an attack zone and a defence zone each round.
    #
    # It is denied on measurement, not on principle, and the gate is not ours
    # to open yet: the page requires `grade >= 3` and `currentLocationId ==
    # "city_11"`. Grade 3 is level 30 plus 25 imperial seals; the fleet tops
    # out at level 24. The entry ticket is gold, not diamonds — the first run
    # of each day is free, and after that 243 * 9^(grade-3) * 2^(runs today) —
    # so this becomes worth measuring the moment a wallet reaches grade 3.
    # Progress persists: a run restarts at the last cleared multiple of ten
    # waves, and `cities/11/vanguard` keeps a top-ten board by waves.
    "startVanguardBattle": "wave dungeon needs grade 3; no reward model yet",
    "vanguardProcessTurn": "wave dungeon needs grade 3; no reward model yet",
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
