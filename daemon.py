#!/usr/bin/env python3
"""SLCW fleet daemon.

Runs the wallet workers and the Telegram control plane in one process. If the vault
passphrase is not supplied through the environment, the fleet stays idle until the
operator unlocks it from Telegram — the engine never touches the game with locked
credentials.
"""
from __future__ import annotations

import os
import signal
import sys
import threading

from bot.telegram import TelegramBot
from slcw import config as config_mod
from slcw.notify import Notifier
from slcw.runner import Fleet
from slcw.vault import Vault, VaultError


def main() -> int:
    config = config_mod.load()
    problems = config.validate()
    for problem in problems:
        print(f"config: {problem}", flush=True)

    if not config.telegram_token or not config.telegram_chat_id:
        print("fatal: Telegram credentials missing; nothing could control this daemon",
              flush=True)
        return 2

    vault = Vault()
    notifier = Notifier(config.telegram_token, config.telegram_chat_id)
    fleet = Fleet(config, vault, notifier)
    bot = TelegramBot(config, vault, fleet)

    passphrase = os.environ.get("SLCW_VAULT_PASSPHRASE", "")
    if passphrase:
        try:
            count = vault.unlock(passphrase)
            migrated = vault.import_legacy(passphrase)
            print(f"vault unlocked from environment: {count + migrated} wallets", flush=True)
            fleet.start()
        except VaultError as exc:
            print(f"vault unlock failed: {exc}", flush=True)
    else:
        print("vault locked — waiting for /unlock from Telegram", flush=True)
        notifier.send("🔐 <b>SLCW daemon start</b>\n\n"
                      "Vault terkunci. Kirim <code>/unlock passphrase-kamu</code> "
                      "untuk mulai.")

    fleet.persist()

    def shutdown(signum, _frame):
        print(f"signal {signum}: stopping fleet", flush=True)
        fleet.stop()
        bot.running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    poller = threading.Thread(target=bot.run, name="slcw-telegram", daemon=True)
    poller.start()

    try:
        while bot.running or poller.is_alive():
            poller.join(timeout=1.0)
    except KeyboardInterrupt:
        shutdown("SIGINT", None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
