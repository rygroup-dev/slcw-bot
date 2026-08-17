"""Interactive first-run setup.

Collects credentials, creates the encrypted vault, adds the first wallet, decides
how the vault unlocks after a restart, and installs the systemd unit.

Re-running is safe: anything already configured is detected and left alone.
"""
from __future__ import annotations

import getpass
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from . import keys
from .config import DATA, ROOT
from .vault import Vault, VaultError

ENV_PATH = ROOT / ".env"
VAULT_KEY_PATH = ROOT / ".vault-key"
UNIT_NAME = "slcw-fleet.service"
UNIT_SOURCE = ROOT / "slcw-fleet.service"
UNIT_TARGET = Path("/etc/systemd/system") / UNIT_NAME

PASSPHRASE_WORDS = (
    "harbor", "cinder", "lattice", "quarry", "meridian", "thistle", "copper",
    "vellum", "brine", "ashen", "cobalt", "furrow", "gantry", "hollow", "ingot",
    "juniper", "kestrel", "lantern", "mortar", "nimbus", "orchard", "pewter",
)

REQUIRED_ENV = (
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token (from @BotFather)"),
    ("TELEGRAM_CHAT_ID", "Your Telegram chat/user id (from @userinfobot)"),
    ("SLCW_FIREBASE_API_KEY", "Firebase web API key (public key from the game frontend)"),
)


# --- presentation --------------------------------------------------------

def heading(text: str) -> None:
    print(f"\n\033[1;36m{text}\033[0m")
    print("\033[2m" + "─" * max(24, len(text)) + "\033[0m")


def ok(text: str) -> None:
    print(f"  \033[1;32m✓\033[0m {text}")


def note(text: str) -> None:
    print(f"    \033[2m{text}\033[0m")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


def choose(prompt: str, options: list[str], default: int = 1) -> int:
    """Present a numbered menu and return the 1-based choice."""
    print()
    for index, option in enumerate(options, start=1):
        print(f"    \033[1m{index}\033[0m) {option}")
    while True:
        raw = input(f"\n  {prompt} [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        print(f"    pick a number between 1 and {len(options)}")


# --- pure helpers (unit-tested) ------------------------------------------

def generate_passphrase(word_count: int = 5) -> str:
    words = [secrets.choice(PASSPHRASE_WORDS) for _ in range(word_count)]
    return "-".join(words) + f"-{secrets.randbelow(9000) + 1000}"


def parse_env(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def read_env() -> dict:
    return parse_env(ENV_PATH.read_text()) if ENV_PATH.exists() else {}


def merge_env(existing: dict, updates: dict) -> dict:
    """Updates win, but never blank out a value that is already set."""
    merged = dict(existing)
    for key, value in updates.items():
        if value:
            merged[key] = value
    return merged


def render_env(values: dict) -> str:
    lines = ["# Written by slcwctl setup. Every key here is read by slcw/config.py.", ""]
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SLCW_FIREBASE_API_KEY"):
        lines.append(f"{key}={values.get(key, '')}")
    lines += ["", "# --- master switches ---",
              f"SLCW_ENABLED={values.get('SLCW_ENABLED', 'true')}",
              f"SLCW_DRY_RUN={values.get('SLCW_DRY_RUN', 'false')}",
              "", "# --- pacing (seconds) ---",
              f"SLCW_REACTION_MEDIAN_SECONDS={values.get('SLCW_REACTION_MEDIAN_SECONDS', '90')}",
              f"SLCW_REACTION_MIN_SECONDS={values.get('SLCW_REACTION_MIN_SECONDS', '15')}",
              f"SLCW_REACTION_MAX_SECONDS={values.get('SLCW_REACTION_MAX_SECONDS', '480')}",
              f"SLCW_IDLE_MIN_SECONDS={values.get('SLCW_IDLE_MIN_SECONDS', '240')}",
              f"SLCW_IDLE_MAX_SECONDS={values.get('SLCW_IDLE_MAX_SECONDS', '900')}",
              "", "# --- resilience ---",
              f"SLCW_MAX_ERRORS={values.get('SLCW_MAX_ERRORS', '3')}",
              f"SLCW_HTTP_MAX_ATTEMPTS={values.get('SLCW_HTTP_MAX_ATTEMPTS', '4')}",
              f"SLCW_HTTP_BACKOFF_BASE={values.get('SLCW_HTTP_BACKOFF_BASE', '1.6')}",
              f"SLCW_HTTP_TIMEOUT={values.get('SLCW_HTTP_TIMEOUT', '30')}",
              "", "# --- gameplay policy ---",
              f"SLCW_REST_HP_RATIO={values.get('SLCW_REST_HP_RATIO', '0.55')}",
              f"SLCW_REST_MP_RATIO={values.get('SLCW_REST_MP_RATIO', '0.25')}",
              f"SLCW_BATTLE_MAX_TURNS={values.get('SLCW_BATTLE_MAX_TURNS', '12')}",
              f"SLCW_MARKET_TTL_SECONDS={values.get('SLCW_MARKET_TTL_SECONDS', '1800')}",
              f"SLCW_FARMING_GOLD_HOURS={values.get('SLCW_FARMING_GOLD_HOURS', '8')}",
              f"SLCW_GOLD_RESERVE={values.get('SLCW_GOLD_RESERVE', '500')}",
              f"SLCW_RICH_DROP_GOLD={values.get('SLCW_RICH_DROP_GOLD', '2000')}",
              ""]
    return "\n".join(lines)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def missing_credentials(values: dict) -> list[str]:
    return [key for key, _ in REQUIRED_ENV if not values.get(key)]


# --- steps ---------------------------------------------------------------

def step_credentials() -> dict:
    heading("1/5  Credentials")
    values = read_env()
    missing = missing_credentials(values)

    if not missing:
        ok("Credentials already present in .env")
        for key, _ in REQUIRED_ENV:
            note(f"{key} = {'*' * 8}{values[key][-4:]}")
        if ask("Replace them? (y/N)", "n").lower() != "y":
            return values
        missing = [key for key, _ in REQUIRED_ENV]

    updates = {}
    for key, description in REQUIRED_ENV:
        if key not in missing:
            continue
        print()
        note(description)
        while True:
            entered = getpass.getpass(f"  {key}: ").strip()
            if entered:
                updates[key] = entered
                break
            print("    required")

    values = merge_env(values, updates)
    write_private(ENV_PATH, render_env(values))
    ok(f"Wrote {ENV_PATH} (0600)")
    return values


def step_vault() -> tuple[Vault, str]:
    heading("2/5  Encrypted vault")
    vault = Vault()

    if vault.exists:
        ok("Vault already exists")
        for _ in range(3):
            passphrase = getpass.getpass("  Vault passphrase: ")
            try:
                count = vault.unlock(passphrase)
            except VaultError as exc:
                print(f"    \033[1;31m{exc}\033[0m")
                continue
            ok(f"Unlocked, {count} wallet(s) inside")
            return vault, passphrase
        raise SystemExit("could not unlock the vault")

    print()
    note("Wallet private keys are stored AES-256-GCM encrypted with this passphrase.")
    choice = choose("How should the passphrase be set?", [
        "Generate a strong one for me (recommended)",
        "I will type my own",
    ])

    if choice == 1:
        passphrase = generate_passphrase()
        print(f"\n  \033[1;33mPassphrase:\033[0m \033[1m{passphrase}\033[0m")
        note("Write this down now. It is the only way to decrypt your wallets.")
        input("\n  Press Enter once you have saved it: ")
    else:
        while True:
            passphrase = getpass.getpass("  New passphrase (10+ chars): ")
            if len(passphrase) < 10:
                print("    too short")
                continue
            if passphrase != getpass.getpass("  Confirm: "):
                print("    they do not match")
                continue
            break

    vault.unlock(passphrase)
    migrated = vault.import_legacy(passphrase)
    if migrated:
        ok(f"Migrated {migrated} wallet(s) from an old plaintext wallets.json")
    else:
        vault.create_wallets(0)
    ok("Vault created")
    return vault, passphrase


def step_unlock_mode(passphrase: str) -> bool:
    """Choose between unattended start and a passphrase prompt on every restart."""
    heading("3/5  Unlock behaviour")
    print()
    note("The vault must be unlocked before the bot can play.")
    choice = choose("What should happen after a reboot?", [
        "Unlock automatically (recommended) — the bot restarts on its own",
        "Ask me in Telegram every time — nothing is stored on disk",
    ])

    if choice == 1:
        write_private(VAULT_KEY_PATH, f"SLCW_VAULT_PASSPHRASE={passphrase}\n")
        ok(f"Auto-unlock enabled via {VAULT_KEY_PATH} (0600)")
        note("The bot now survives reboots without you touching it.")
        note("Anyone with root on this box can read that file; that is the trade.")
        return True

    if VAULT_KEY_PATH.exists():
        VAULT_KEY_PATH.unlink()
    ok("Manual unlock — send /unlock <passphrase> in Telegram after each restart")
    return False


def step_wallet(vault: Vault) -> None:
    heading("4/5  Your first wallet")
    existing = vault.wallets()
    if existing:
        ok(f"{len(existing)} wallet(s) already in the vault")
        for wallet in vault.public_summary():
            note(f"{wallet['id']}  {wallet['nickname']}  {wallet['public_key']}")
        if ask("Add another? (y/N)", "n").lower() != "y":
            return

    choice = choose("How do you want to add a wallet?", [
        "Import an existing one — paste a seed phrase or private key",
        "Create a new one automatically",
    ])

    if choice == 1:
        _import_wallet(vault)
    else:
        count = ask("How many wallets to create?", "1")
        created = vault.create_wallets(max(1, int(count) if count.isdigit() else 1))
        print()
        for wallet in created:
            ok(f"{wallet['id']}  {wallet['nickname']}")
            note(wallet["public_key"])
        note("These accounts need no SOL. The game accepts a bare public key, and")
        note("in-game onboarding runs automatically on the first cycle.")


def _import_wallet(vault: Vault) -> None:
    print()
    note("Accepted: 12/24-word seed phrase, base58, JSON byte array, or hex.")
    note("Input is hidden and never written to shell history.")

    for _ in range(3):
        secret = getpass.getpass("  Secret: ").strip()
        if not secret:
            continue
        passphrase = ""
        if len(secret.split()) >= 12:
            passphrase = getpass.getpass("  BIP39 passphrase (blank if none): ")

        try:
            candidates = keys.parse_secret(secret, passphrase)
        except keys.KeyImportError as exc:
            print(f"    \033[1;31m{exc}\033[0m")
            continue

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            # A seed phrase is not one account: wallet apps derive at
            # m/44'/501'/0'/0' while solana-keygen uses the bare BIP39 seed.
            print()
            note("This phrase matches more than one address. Pick the account you own —")
            note("the wrong one would log in fine and then play an empty profile.")
            index = choose("Which address is yours?",
                           [f"{c.source}\n       {c.public_key}" for c in candidates])
            chosen = candidates[index - 1]

        try:
            wallet = vault.import_wallet(chosen.private_key, chosen.public_key)
        except ValueError as exc:
            print(f"    \033[1;31m{exc}\033[0m")
            continue

        print()
        ok(f"Imported {wallet['id']}  {wallet['nickname']}")
        note(wallet["public_key"])
        return

    raise SystemExit("import failed three times")


def step_service(auto_unlock: bool) -> None:
    heading("5/5  Background service")

    if not shutil.which("systemctl"):
        ok("systemd not present — start manually with .venv/bin/python daemon.py")
        return

    unit = UNIT_SOURCE.read_text()
    if auto_unlock and str(VAULT_KEY_PATH) not in unit:
        # The `-` prefix makes the file optional, so manual-unlock installs that
        # never create it still start cleanly.
        unit = unit.replace(
            "EnvironmentFile=/root/slcw-bot/.env",
            f"EnvironmentFile=/root/slcw-bot/.env\nEnvironmentFile=-{VAULT_KEY_PATH}")
        UNIT_SOURCE.write_text(unit)

    UNIT_TARGET.write_text(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--quiet", UNIT_NAME], check=True)
    subprocess.run(["systemctl", "restart", UNIT_NAME], check=True)

    state = subprocess.run(["systemctl", "is-active", UNIT_NAME],
                           capture_output=True, text=True).stdout.strip()
    if state == "active":
        ok(f"{UNIT_NAME} running and enabled at boot")
    else:
        print(f"  \033[1;31m✗\033[0m {UNIT_NAME} is {state} — check journalctl -u {UNIT_NAME}")


def summary(vault: Vault, auto_unlock: bool) -> None:
    heading("Ready")
    wallets = vault.wallets() if vault.is_unlocked else []
    print(f"""
  Wallets      {len(wallets)}
  Vault        {'auto-unlock on boot' if auto_unlock else 'unlock via Telegram each restart'}
  Service      systemctl status slcw-fleet
  Logs         journalctl -u slcw-fleet -f
  Admin        ./slcwctl doctor | list | new N | import | why <id>

  Open Telegram and send /menu.""")
    if not auto_unlock:
        print("  Send /unlock <passphrase> first — the bot is idle until you do.")
    print()


def run() -> int:
    print("\n\033[1m  SLCW fleet setup\033[0m")
    step_credentials()
    vault, passphrase = step_vault()
    auto_unlock = step_unlock_mode(passphrase)
    step_wallet(vault)
    step_service(auto_unlock)
    summary(vault, auto_unlock)
    return 0
