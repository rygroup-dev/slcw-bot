"""Outbound Telegram messaging.

Kept separate from the interactive bot so engine threads can push alerts without
importing the UI layer.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.enabled = bool(token and chat_id)

    def _post(self, method: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}", data=data, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def send(self, text: str, **extra) -> dict | None:
        if not self.enabled:
            return None
        payload = {"chat_id": self.chat_id, "text": text[:4000],
                   "parse_mode": "HTML", "disable_web_page_preview": "true"}
        payload.update(extra)
        try:
            return self._post("sendMessage", payload)
        except Exception:
            # Alerting must never take the engine down.
            return None


class NullNotifier(Notifier):
    def __init__(self):
        super().__init__("", "")

    def send(self, text: str, **extra):
        return None


class Alerts:
    """Decides which events are worth interrupting the operator for.

    A bot that reports every cycle trains its owner to ignore it, so routine
    actions stay in the dashboard and only these categories push a message.
    """

    def __init__(self, notifier: Notifier):
        self.notifier = notifier
        self._seen: set = set()

    def _once(self, key: str) -> bool:
        """Rate-limit by event identity so a stuck condition alerts one time."""
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def clear(self, key_prefix: str) -> None:
        self._seen = {k for k in self._seen if not k.startswith(key_prefix)}

    # --- failure conditions ---------------------------------------------
    def circuit_breaker(self, wallet_id: str, nickname: str, error: str) -> None:
        self.notifier.send(
            f"🛑 <b>{wallet_id}</b> ({nickname}) dihentikan circuit breaker\n"
            f"<code>{_clip(error)}</code>\n\n"
            f"Buka ⚙️ Kontrol → ▶️ Resume setelah diperiksa.")

    def auth_failure(self, wallet_id: str, error: str) -> None:
        if self._once(f"auth:{wallet_id}"):
            self.notifier.send(
                f"🔑 <b>{wallet_id}</b> gagal autentikasi\n<code>{_clip(error)}</code>")

    def guardrail(self, wallet_id: str, error: str) -> None:
        self.notifier.send(
            f"🚧 <b>{wallet_id}</b> mencoba aksi terlarang dan diblokir\n"
            f"<code>{_clip(error)}</code>")

    def vault_locked(self) -> None:
        if self._once("vault"):
            self.notifier.send(
                "🔐 <b>Vault terkunci</b>\n\nEngine idle. "
                "Kirim <code>/unlock passphrase-kamu</code> untuk melanjutkan.")

    # --- opportunities ---------------------------------------------------
    def level_up(self, wallet_id: str, level: int) -> None:
        self.notifier.send(f"⬆️ <b>{wallet_id}</b> naik ke level {level}")

    def clan_quest(self, wanted: str, quest_id: str, clan_xp: int) -> None:
        """A new clan quest, announced once per quest.

        The server answers a second generateClanQuest with "cooldown_active",
        which the engine classifies as benign — so the call returns a success
        carrying no quest, and the leader used to re-announce on every cycle.
        A quest with no requirements is that empty answer, not news.
        """
        if not wanted:
            return
        if self._once(f"clanquest:{quest_id or wanted}"):
            self.notifier.send(
                f"\U0001F4DC quest clan baru: <b>{wanted}</b>\n"
                f"+{clan_xp:,} clan XP kalau selesai "
                f"(seat bertambah tiap level)")

    def crossed_market(self, books: list) -> None:
        rows = "\n".join(
            f"  {b.template_id}: bid {b.best_bid:,.0f} / ask {b.best_ask:,.0f} "
            f"→ margin {abs(b.spread):,.0f}" for b in books[:5])
        key = "crossed:" + ",".join(sorted(b.template_id for b in books[:5]))
        if self._once(key):
            self.notifier.send(
                f"⚡ <b>Spread crossed terdeteksi</b>\n{rows}\n\n"
                f"Bot tidak memasang order sendiri — ini perlu keputusan kamu.")

    def rich_drop(self, wallet_id: str, item: str, quantity: int, value: float) -> None:
        self.notifier.send(
            f"💎 <b>{wallet_id}</b> dapat {item} ×{quantity} "
            f"(≈{value:,.0f} gold di best-bid)")

    def low_energy_idle(self, wallet_id: str) -> None:
        if self._once(f"idle:{wallet_id}"):
            self.notifier.send(
                f"😴 <b>{wallet_id}</b> tidak punya aksi menguntungkan di lokasinya. "
                f"Pertimbangkan pindah lokasi.")


def _clip(text: str, limit: int = 300) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"
