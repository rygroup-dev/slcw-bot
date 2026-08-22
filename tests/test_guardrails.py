import unittest

from slcw import guardrails
from slcw.config import Config
from slcw.transport import Transport


class GuardrailTests(unittest.TestCase):
    def test_allows_known_safe_callable(self):
        guardrails.check("finishActivity")  # must not raise

    def test_denies_diamond_spending(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("spendDiamonds")

    def test_denies_withdrawal(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("withdraw")

    def test_denies_transaction_signing(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("signTransaction")

    def test_denies_market_order_creation(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("createOrder")

    def test_denies_speedup(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("speedUp")

    def test_denies_skip_activity_time(self):
        # The frontend charges 5 * ceil(seconds_left / 60) diamonds for this.
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("skipActivityTime")

    def test_denies_arena_queue(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("joinArenaQueue")

    def test_allows_free_level_up(self):
        """Only the diamond booster costs anything; slcw.leveling pins it to none."""
        guardrails.check("buyLevel")

    def test_allows_gathering(self):
        guardrails.check("startFarming")

    def test_unknown_callable_denied_by_default(self):
        with self.assertRaises(guardrails.GuardrailViolation):
            guardrails.check("someBrandNewCallable")

    def test_allowlist_and_denylist_never_overlap(self):
        self.assertEqual(
            guardrails.ALLOWED_CALLABLES & set(guardrails.DENIED_CALLABLES), set())

    def test_every_denial_records_a_reason(self):
        for name, reason in guardrails.DENIED_CALLABLES.items():
            self.assertTrue(reason, f"{name} is denied without a stated reason")

    def test_denial_message_explains_itself(self):
        with self.assertRaises(guardrails.GuardrailViolation) as ctx:
            guardrails.check("refillEnergyPaid")
        self.assertIn("diamonds", str(ctx.exception))

    def test_paid_energy_denied_while_free_energy_is_allowed(self):
        # The paid call costs 99 * 2^(refills today) diamonds for something the
        # free call provides three times a day.
        self.assertTrue(guardrails.is_allowed("refillEnergyFree"))
        self.assertFalse(guardrails.is_allowed("refillEnergyPaid"))

    def test_refining_and_crafting_allowed(self):
        for name in ("startRefining", "startCrafting", "startHunting"):
            self.assertTrue(guardrails.is_allowed(name), name)

    def test_gold_market_trading_denied(self):
        for name in ("placeGoldOrder", "cancelGoldOrder", "executeGoldMarketOrder"):
            self.assertFalse(guardrails.is_allowed(name), name)

    def test_irreversible_item_consumption_denied(self):
        for name in ("sharpenItem", "awakenItem",
                     "startSoulExtraction", "deleteInventoryItem"):
            self.assertFalse(guardrails.is_allowed(name), name)

    def test_the_grade_path_is_allowed_on_an_operator_decision(self):
        """evolveGrade really does spend seals for good, and it is allowed
        anyway: a grade-1 character is capped at level 15 and discards every
        point of XP it earns after that. The other two were denied on wrong
        information — purchaseImperialSeal is paid in gold, not diamonds, and
        payCityEntryFee buys the fifty-gold door those two stand behind."""
        for name in ("evolveGrade", "purchaseImperialSeal", "payCityEntryFee"):
            self.assertTrue(guardrails.is_allowed(name), name)

    def test_unmeasured_systems_denied(self):
        for name in ("startExpedition", "joinArenaQueue", "dispatchCaravan",
                     "handleReferral"):
            self.assertFalse(guardrails.is_allowed(name), name)

    def test_free_quest_flow_allowed(self):
        for name in ("getTaskStatus", "acceptTask", "claimTaskReward",
                     "generateMiningQuests", "completeMiningQuest"):
            self.assertTrue(guardrails.is_allowed(name), name)

    def test_every_known_callable_is_classified(self):
        """Every callable found in the game bundle must be a deliberate decision."""
        discovered = {
            "acceptTask", "awakenItem", "becomeCitizen", "buyBiomantPass", "buyLevel",
            "cancelGoldOrder", "claimExpeditionRewards", "claimInitialReward",
            "claimTaskReward", "completeCitizenshipQuest", "completeMiningQuest",
            "completeNewbieQuest", "deleteInventoryItem", "dispatchCaravan",
            "equipItem", "equipMount", "evolveGrade", "executeGoldMarketOrder",
            "expandInventory", "finishActivity", "finishExpedition",
            "generateCitizenshipQuests", "generateMiningQuests", "getSolanaNonce",
            "getTaskStatus", "handleReferral", "initializeImperialStats",
            "initializeNewInventory", "instantCompleteMiningQuest", "joinArenaQueue",
            "leaveArenaQueue", "linkSolanaWallet", "migrateToNewInventory",
            "openChests", "payCityEntryFee", "placeGoldOrder", "premiumSearchMount",
            "purchaseImperialSeal", "purchaseInitialRewardsPass",
            "purchaseMiningGoldSlot", "purchaseStarterPack", "refillEnergyFree",
            "refillEnergyPaid", "renounceCitizenship", "resetPoints",
            "restoreMountStamina", "searchMountActivity", "sharpenItem",
            "skipActivityTime", "sortInventory", "spendAttributePoints",
            "startBattle", "startCrafting", "startExpedition", "startFarming",
            "startFirstTravel", "startHunting", "startProduction", "startRefining",
            "startSoulExtraction", "startTaskBattle", "startTravel", "unequipItem",
            "updateDisplayName", "upgradeBiomantTalent", "upgradeMiningQuests",
            "upgradeMounts", "verifySolanaLogin",
        }
        classified = guardrails.ALLOWED_CALLABLES | set(guardrails.DENIED_CALLABLES)
        unclassified = discovered - classified
        self.assertEqual(unclassified, set(),
                         f"callables found in the game but never classified: "
                         f"{sorted(unclassified)}")

    def test_is_allowed_helper(self):
        self.assertTrue(guardrails.is_allowed("startRelax"))
        self.assertFalse(guardrails.is_allowed("withdraw"))


class TransportEnforcementTests(unittest.TestCase):
    """The guard must sit in the transport, not only in the caller."""

    def test_denied_callable_never_reaches_the_network(self):
        transport = Transport(config=Config())
        called = []
        transport.request = lambda *a, **k: called.append(a)  # type: ignore

        with self.assertRaises(guardrails.GuardrailViolation):
            transport.call_function("withdraw", {}, "token")
        self.assertEqual(called, [], "a denied callable must not issue a request")


if __name__ == "__main__":
    unittest.main()
