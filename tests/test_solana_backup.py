"""Fund movement and key export. No network, no real transfers."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solders.keypair import Keypair

from slcw import backup, solana
from slcw import vault as vault_mod
from slcw.vault import Vault


def wallet(index: int, keypair=None) -> dict:
    keypair = keypair or Keypair()
    return {"id": f"wallet-{index:02d}", "nickname": f"n{index}",
            "public_key": str(keypair.pubkey()), "private_key": str(keypair)}


class AmountParsingTests(unittest.TestCase):
    def test_plain_decimal(self):
        self.assertAlmostEqual(solana.parse_amount("0.05"), 0.05)

    def test_comma_decimal_is_accepted(self):
        """An Indonesian keyboard types 0,05 — misreading it moves 1000x."""
        self.assertAlmostEqual(solana.parse_amount("0,05"), 0.05)

    def test_unit_suffix_is_tolerated(self):
        self.assertAlmostEqual(solana.parse_amount("0.5 SOL"), 0.5)

    def test_rejects_zero_and_negative(self):
        for bad in ("0", "-1", "0.0"):
            with self.assertRaises(ValueError):
                solana.parse_amount(bad)

    def test_rejects_nonsense(self):
        for bad in ("", "   ", "abc", "1.2.3"):
            with self.assertRaises(ValueError):
                solana.parse_amount(bad)

    def test_rejects_an_implausible_amount(self):
        with self.assertRaises(ValueError):
            solana.parse_amount("5000")


class ConversionTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(solana.sol_to_lamports(1), solana.LAMPORTS_PER_SOL)
        self.assertAlmostEqual(solana.lamports_to_sol(solana.LAMPORTS_PER_SOL), 1.0)

    def test_small_amounts_do_not_lose_precision(self):
        self.assertEqual(solana.sol_to_lamports(0.000001), 1000)


class DistributionPlanTests(unittest.TestCase):
    def setUp(self):
        self.source = wallet(1)
        self.wallets = [self.source] + [wallet(i) for i in range(2, 6)]

    def _plan(self, amount, balance):
        return solana.plan_distribution(self.source, self.wallets, amount, balance)

    def test_source_is_excluded_from_its_own_distribution(self):
        plan = self._plan(0.01, solana.sol_to_lamports(1))
        self.assertEqual(plan.count, 4)
        self.assertNotIn(self.source["public_key"],
                         [w["public_key"] for w in plan.recipients])

    def test_totals_include_fees_and_the_rent_reserve(self):
        plan = self._plan(0.01, solana.sol_to_lamports(1))
        self.assertEqual(plan.total_lamports, solana.sol_to_lamports(0.01) * 4)
        self.assertEqual(plan.fee_lamports, solana.BASE_FEE_LAMPORTS * 4)
        self.assertEqual(plan.required_lamports,
                         plan.total_lamports + plan.fee_lamports
                         + solana.RENT_EXEMPT_LAMPORTS)

    def test_affordable_when_funded(self):
        self.assertTrue(self._plan(0.01, solana.sol_to_lamports(1)).affordable)

    def test_unaffordable_reports_the_exact_shortfall(self):
        plan = self._plan(1.0, solana.sol_to_lamports(1))
        self.assertFalse(plan.affordable)
        self.assertEqual(plan.shortfall_lamports,
                         plan.required_lamports - plan.source_balance)

    def test_rent_reserve_alone_can_make_it_unaffordable(self):
        """A balance exactly equal to the transfers still cannot be sent."""
        plan = self._plan(0.01, solana.sol_to_lamports(0.04))
        self.assertFalse(plan.affordable)

    def test_execution_refuses_an_unaffordable_plan(self):
        plan = self._plan(1.0, 0)
        with self.assertRaises(solana.SolanaError):
            solana.execute_distribution(None, self.source, plan)


class SweepPlanTests(unittest.TestCase):
    def setUp(self):
        self.primary = wallet(1)
        self.others = [wallet(i) for i in range(2, 5)]
        self.wallets = [self.primary] + self.others

    def test_sweepable_leaves_rent_and_fee_behind(self):
        balance = solana.RENT_EXEMPT_LAMPORTS + solana.BASE_FEE_LAMPORTS + 1234
        self.assertEqual(solana.sweepable_lamports(balance), 1234)

    def test_dust_yields_nothing(self):
        self.assertEqual(solana.sweepable_lamports(solana.RENT_EXEMPT_LAMPORTS), 0)
        self.assertEqual(solana.sweepable_lamports(0), 0)

    def test_destination_is_never_swept_into_itself(self):
        balances = {w["public_key"]: solana.sol_to_lamports(1) for w in self.wallets}
        plan = solana.plan_sweep(self.wallets, self.primary, balances)
        self.assertEqual(plan.count, 3)
        self.assertNotIn(self.primary["public_key"],
                         [w["public_key"] for w, _ in plan.entries])

    def test_empty_wallets_are_reported_as_skipped(self):
        balances = {w["public_key"]: 0 for w in self.wallets}
        balances[self.others[0]["public_key"]] = solana.sol_to_lamports(1)
        plan = solana.plan_sweep(self.wallets, self.primary, balances)
        self.assertEqual(plan.count, 1)
        self.assertEqual(len(plan.skipped), 2)

    def test_total_is_the_sum_of_entries(self):
        balances = {w["public_key"]: solana.sol_to_lamports(0.5) for w in self.wallets}
        plan = solana.plan_sweep(self.wallets, self.primary, balances)
        self.assertEqual(plan.total_lamports,
                         sum(amount for _, amount in plan.entries))


class RpcTests(unittest.TestCase):
    class FakeSession:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append(kwargs.get("json"))
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def close(self):
            pass

    class FakeResponse:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload) if payload else ""

        def json(self):
            return self._payload

    def _client(self, *responses):
        client = solana.SolanaClient("https://rpc.test")
        client._session = self.FakeSession(*responses)
        return client

    def test_balance_is_read(self):
        client = self._client(self.FakeResponse(200, {"result": {"value": 12345}}))
        self.assertEqual(client.balance("PUB"), 12345)

    def test_rate_limit_is_retried(self):
        client = self._client(self.FakeResponse(429),
                              self.FakeResponse(200, {"result": {"value": 7}}))
        with patch("slcw.solana.time.sleep"):
            self.assertEqual(client.balance("PUB"), 7)
        self.assertEqual(len(client._session.calls), 2)

    def test_rate_limit_inside_a_200_body_is_also_retried(self):
        client = self._client(
            self.FakeResponse(200, {"error": {"message": "Too many requests, rate"}}),
            self.FakeResponse(200, {"result": {"value": 9}}))
        with patch("slcw.solana.time.sleep"):
            self.assertEqual(client.balance("PUB"), 9)

    def test_a_real_rpc_error_is_raised_not_retried(self):
        client = self._client(
            self.FakeResponse(200, {"error": {"message": "invalid public key"}}))
        with self.assertRaises(solana.SolanaError):
            client.balance("PUB")
        self.assertEqual(len(client._session.calls), 1)

    def test_batched_balances_chunk_the_request(self):
        keys = [f"K{i}" for i in range(150)]
        client = self._client(
            self.FakeResponse(200, {"result": {"value": [{"lamports": 1}] * 100}}),
            self.FakeResponse(200, {"result": {"value": [{"lamports": 2}] * 50}}))
        balances = client.balances(keys)
        self.assertEqual(len(balances), 150)
        self.assertEqual(len(client._session.calls), 2)

    def test_missing_accounts_read_as_zero(self):
        client = self._client(
            self.FakeResponse(200, {"result": {"value": [None, {"lamports": 5}]}}))
        balances = client.balances(["A", "B"])
        self.assertEqual(balances["A"], 0)
        self.assertEqual(balances["B"], 5)

    def test_send_rejects_a_non_positive_amount(self):
        with self.assertRaises(solana.SolanaError):
            self._client().send_sol(Keypair(), str(Keypair().pubkey()), 0)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.wallets = [wallet(i) for i in range(1, 4)]

    def test_payload_carries_every_wallet(self):
        payload = backup.export_payload(self.wallets)
        self.assertEqual(payload["wallet_count"], 3)
        self.assertEqual(len(payload["wallets"]), 3)

    def test_payload_warns_about_what_it_contains(self):
        self.assertIn("PRIVATE KEYS", backup.export_payload(self.wallets)["warning"])

    def test_only_restore_fields_are_included(self):
        entry = backup.export_payload(self.wallets)["wallets"][0]
        self.assertEqual(set(entry), {"id", "nickname", "public_key", "private_key"})

    def test_export_verifies_every_key_derives_its_public_key(self):
        self.assertEqual(backup.verify_payload(backup.export_payload(self.wallets)), [])

    def test_verification_catches_a_mismatched_pair(self):
        payload = backup.export_payload(self.wallets)
        payload["wallets"][1]["public_key"] = str(Keypair().pubkey())
        problems = backup.verify_payload(payload)
        self.assertEqual(len(problems), 1)
        self.assertIn("derives", problems[0])

    def test_verification_catches_an_unreadable_key(self):
        payload = backup.export_payload(self.wallets)
        payload["wallets"][0]["private_key"] = "not-a-key"
        self.assertTrue(backup.verify_payload(payload))

    def test_empty_export_is_reported(self):
        self.assertTrue(backup.verify_payload({"wallets": []}))

    def test_export_round_trips_back_into_a_vault(self):
        """A backup nobody can restore is not a backup."""
        exported = json.loads(backup.export_json(self.wallets))
        for entry in exported["wallets"]:
            derived = Keypair.from_base58_string(entry["private_key"])
            self.assertEqual(str(derived.pubkey()), entry["public_key"])

    def test_filename_is_dated_and_counted(self):
        name = backup.export_filename(7)
        self.assertIn("-7-", name)
        self.assertTrue(name.endswith(".json"))


class PrimaryWalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = patch.object(vault_mod, "VAULT_PATH",
                                  Path(self.tmp.name) / "wallets.enc")
        self._path.start()
        self._n = patch.object(vault_mod, "SCRYPT_N", 2 ** 12)
        self._n.start()
        self.vault = Vault()
        self.vault.unlock("passphrase for tests")

    def tearDown(self):
        self._n.stop()
        self._path.stop()
        self.tmp.cleanup()

    def test_no_primary_in_an_empty_vault(self):
        self.assertIsNone(self.vault.primary())

    def test_first_created_wallet_becomes_primary(self):
        created = self.vault.create_wallets(3)
        self.assertEqual(self.vault.primary()["id"], created[0]["id"])
        self.assertTrue(created[0]["is_primary"])
        self.assertFalse(created[1]["is_primary"])

    def test_first_imported_wallet_becomes_primary(self):
        keypair = Keypair()
        imported = self.vault.import_wallet(str(keypair), str(keypair.pubkey()))
        self.assertTrue(imported["is_primary"])
        self.assertEqual(self.vault.primary()["id"], imported["id"])

    def test_later_wallets_do_not_steal_the_flag(self):
        first = self.vault.create_wallets(1)[0]
        self.vault.create_wallets(2)
        self.assertEqual(self.vault.primary()["id"], first["id"])

    def test_primary_can_be_moved(self):
        created = self.vault.create_wallets(3)
        self.vault.set_primary(created[2]["id"])
        self.assertEqual(self.vault.primary()["id"], created[2]["id"])

    def test_exactly_one_wallet_holds_the_flag(self):
        created = self.vault.create_wallets(4)
        self.vault.set_primary(created[3]["id"])
        flagged = [w for w in self.vault.wallets() if w.get("is_primary")]
        self.assertEqual(len(flagged), 1)

    def test_moving_to_an_unknown_wallet_is_rejected(self):
        self.vault.create_wallets(1)
        with self.assertRaises(KeyError):
            self.vault.set_primary("wallet-99")

    def test_primary_survives_a_reopen(self):
        created = self.vault.create_wallets(3)
        self.vault.set_primary(created[1]["id"])
        reopened = Vault()
        reopened.unlock("passphrase for tests")
        self.assertEqual(reopened.primary()["id"], created[1]["id"])

    def test_a_vault_predating_the_flag_still_answers(self):
        """Existing vaults have no is_primary field and must not break."""
        self.vault.create_wallets(2)
        for entry in self.vault.wallets():
            entry.pop("is_primary", None)
        self.assertIsNotNone(self.vault.primary())

    def test_summary_marks_the_primary(self):
        created = self.vault.create_wallets(2)
        summary = self.vault.public_summary()
        self.assertTrue(summary[0]["is_primary"])
        self.assertFalse(summary[1]["is_primary"])
        # And still leaks nothing.
        self.assertNotIn("private_key", summary[0])


if __name__ == "__main__":
    unittest.main()
