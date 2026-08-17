import binascii
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solders.keypair import Keypair

from slcw import keys, vault as vault_mod
from slcw.vault import Vault


class Slip10Tests(unittest.TestCase):
    """Official SLIP-0010 ed25519 test vector 1.

    Phantom derives at m/44'/501'/0'/0' while solana-keygen uses the bare BIP39
    seed, so this derivation is what decides whether an imported seed phrase lands
    on the account the operator actually owns.
    """

    SEED = binascii.unhexlify("000102030405060708090a0b0c0d0e0f")

    def test_master_key_and_chain_code(self):
        key, chain = keys._slip10_master(self.SEED)
        self.assertEqual(
            key.hex(), "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7")
        self.assertEqual(
            chain.hex(), "90046a93de5380a72b5e45010748567d5ea02bbf6522f979e05c0d8d8ca9fffb")

    def test_first_hardened_child(self):
        key, chain = keys._slip10_master(self.SEED)
        child, _ = keys._slip10_child(key, chain, 0)
        self.assertEqual(
            child.hex(), "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3")

    def test_two_level_path(self):
        self.assertEqual(
            keys.derive_slip10(self.SEED, "m/0'/1'").hex(),
            "b1d0bad404bf35da785a64ca1ac54b2617211d2777696fbffaf208f746ae84f2")


class Bip39Tests(unittest.TestCase):
    def test_matches_the_trezor_reference_vector(self):
        seed = keys.mnemonic_to_seed(
            "legal winner thank year wave sausage worth useful legal winner thank yellow",
            "TREZOR")
        self.assertTrue(seed.hex().startswith(
            "2e8905819b8723fe2c1d161860e5ee1830318dbf49a83bd451cfb8440c28bd6f"))

    def test_passphrase_changes_the_seed(self):
        phrase = "legal winner thank year wave sausage worth useful legal winner thank yellow"
        self.assertNotEqual(keys.mnemonic_to_seed(phrase, ""),
                            keys.mnemonic_to_seed(phrase, "TREZOR"))

    def test_whitespace_and_case_are_normalised(self):
        phrase = "legal winner thank year wave sausage worth useful legal winner thank yellow"
        self.assertEqual(keys.mnemonic_to_seed(phrase),
                         keys.mnemonic_to_seed(f"  {phrase.upper()}   "))


class ParseSecretTests(unittest.TestCase):
    def setUp(self):
        self.keypair = Keypair()
        self.public_key = str(self.keypair.pubkey())
        self.raw = bytes(self.keypair)

    def _assert_resolves(self, text, source):
        candidates = keys.parse_secret(text)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].public_key, self.public_key)
        self.assertEqual(candidates[0].source, source)

    def test_base58(self):
        self._assert_resolves(str(self.keypair), "base58")

    def test_json_byte_array(self):
        self._assert_resolves(json.dumps(list(self.raw)), "json array")

    def test_json_array_with_whitespace(self):
        self._assert_resolves(f"  {json.dumps(list(self.raw))}  ", "json array")

    def test_hex(self):
        self._assert_resolves(self.raw.hex(), "hex")

    def test_hex_with_prefix(self):
        self._assert_resolves("0x" + self.raw.hex(), "hex")

    def test_thirty_two_byte_seed_expands_to_a_keypair(self):
        seed = self.raw[:32]
        candidates = keys.parse_secret(seed.hex())
        self.assertEqual(candidates[0].public_key, self.public_key)

    def test_private_key_round_trips_back_to_the_same_account(self):
        candidate = keys.parse_secret(str(self.keypair))[0]
        restored = Keypair.from_base58_string(candidate.private_key)
        self.assertEqual(str(restored.pubkey()), self.public_key)

    def test_seed_phrase_offers_both_derivations(self):
        phrase = ("legal winner thank year wave sausage worth useful "
                  "legal winner thank yellow")
        candidates = keys.parse_secret(phrase)
        self.assertEqual(len(candidates), 2)
        self.assertIn("m/44'/501'/0'/0'", candidates[0].source)
        self.assertIn("no derivation path", candidates[1].source)
        # Two genuinely different accounts, which is exactly why we ask.
        self.assertNotEqual(candidates[0].public_key, candidates[1].public_key)

    def test_twenty_four_word_phrase_accepted(self):
        phrase = " ".join(["abandon"] * 23 + ["art"])
        self.assertEqual(len(keys.parse_secret(phrase)), 2)

    def test_empty_input_rejected(self):
        with self.assertRaises(keys.KeyImportError):
            keys.parse_secret("   ")

    def test_garbage_rejected(self):
        with self.assertRaises(keys.KeyImportError):
            keys.parse_secret("this is definitely not a key!!!")

    def test_malformed_json_rejected(self):
        with self.assertRaises(keys.KeyImportError):
            keys.parse_secret("[1,2,3")

    def test_out_of_range_bytes_rejected(self):
        with self.assertRaises(keys.KeyImportError):
            keys.parse_secret(json.dumps([999] * 64))

    def test_wrong_length_key_rejected(self):
        with self.assertRaises(keys.KeyImportError):
            keys.parse_secret(json.dumps([1] * 40))

    def test_redact_never_echoes_the_secret(self):
        secret = str(self.keypair)
        self.assertNotIn(secret, keys.redact(secret))


class VaultImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = patch.object(vault_mod, "VAULT_PATH",
                                  Path(self.tmp.name) / "wallets.enc")
        self._path.start()
        self._n = patch.object(vault_mod, "SCRYPT_N", 2 ** 12)
        self._n.start()
        self.vault = Vault()
        self.vault.unlock("passphrase for tests")
        self.keypair = Keypair()

    def tearDown(self):
        self._n.stop()
        self._path.stop()
        self.tmp.cleanup()

    def test_import_adds_a_usable_wallet(self):
        wallet = self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        self.assertEqual(wallet["public_key"], str(self.keypair.pubkey()))
        self.assertTrue(wallet["imported"])
        # An existing account must not be sent through onboarding again.
        self.assertTrue(wallet["onboarded"])
        self.assertIsNotNone(wallet["persona"])

    def test_imported_wallet_survives_a_reopen(self):
        self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        reopened = Vault()
        reopened.unlock("passphrase for tests")
        self.assertEqual(reopened.wallets()[0]["public_key"], str(self.keypair.pubkey()))

    def test_mismatched_public_key_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.vault.import_wallet(str(self.keypair), str(Keypair().pubkey()))
        self.assertIn("derives", str(ctx.exception))

    def test_duplicate_import_is_rejected(self):
        self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        with self.assertRaises(ValueError) as ctx:
            self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        self.assertIn("already in the vault", str(ctx.exception))

    def test_imported_and_generated_wallets_get_distinct_ids(self):
        self.vault.create_wallets(2)
        wallet = self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        ids = [w["id"] for w in self.vault.wallets()]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertIn(wallet["id"], ids)

    def test_import_gets_its_own_sleep_schedule(self):
        from slcw.vault import SLEEP_HOURS_RANGE
        wallet = self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        self.assertIn("sleep_anchor_hour", wallet)
        self.assertGreaterEqual(wallet["sleep_hours"], SLEEP_HOURS_RANGE[0])
        self.assertLessEqual(wallet["sleep_hours"], SLEEP_HOURS_RANGE[1])

    def test_ciphertext_hides_the_imported_key(self):
        self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        self.assertNotIn(str(self.keypair), vault_mod.VAULT_PATH.read_text())

    def test_public_summary_excludes_the_imported_key(self):
        self.vault.import_wallet(str(self.keypair), str(self.keypair.pubkey()))
        self.assertNotIn(str(self.keypair), json.dumps(self.vault.public_summary()))


class GeneratedWalletOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = patch.object(vault_mod, "VAULT_PATH",
                                  Path(self.tmp.name) / "wallets.enc")
        self._path.start()
        self._n = patch.object(vault_mod, "SCRYPT_N", 2 ** 12)
        self._n.start()

    def tearDown(self):
        self._n.stop()
        self._path.stop()
        self.tmp.cleanup()

    def test_generated_wallets_are_flagged_for_onboarding(self):
        vault = Vault()
        vault.unlock("passphrase for tests")
        created = vault.create_wallets(1)
        self.assertFalse(created[0]["onboarded"],
                         "a brand-new account has no game profile until initialised")

    def test_update_clears_the_onboarding_flag(self):
        vault = Vault()
        vault.unlock("passphrase for tests")
        created = vault.create_wallets(1)
        vault.update(created[0]["id"], onboarded=True)
        self.assertTrue(vault.get(created[0]["id"])["onboarded"])
        reopened = Vault()
        reopened.unlock("passphrase for tests")
        self.assertTrue(reopened.get(created[0]["id"])["onboarded"])


if __name__ == "__main__":
    unittest.main()
