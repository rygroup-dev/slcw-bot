"""Game API surface: Firebase callables plus Firestore reads."""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

from .config import FIRESTORE_BASE
from .model import PlayerState, decode_document, parse_player
from .transport import Transport


@dataclass
class GameApi:
    transport: Transport

    # --- Firestore reads -------------------------------------------------
    def _document(self, path: str, id_token: str) -> dict:
        payload = self.transport.request(
            "GET", f"{FIRESTORE_BASE}/{path}",
            headers={"Authorization": f"Bearer {id_token}"},
        )
        return decode_document(payload)

    def get_player(self, session) -> PlayerState:
        uid = urllib.parse.quote(session.local_id, safe="")
        return parse_player(self._document(f"users/{uid}", session.id_token))

    def get_inventory(self, session) -> dict:
        uid = urllib.parse.quote(session.local_id, safe="")
        return self._document(f"inventories/{uid}", session.id_token)

    def query_collection(self, session, collection: str, limit: int = 500,
                         offset: int = 0) -> list[dict]:
        payload = self.transport.request(
            "POST", f"{FIRESTORE_BASE}:runQuery",
            json_body={"structuredQuery": {
                "from": [{"collectionId": collection}],
                "limit": limit,
                "offset": offset,
            }},
            headers={"Authorization": f"Bearer {session.id_token}"},
        )
        rows = payload if isinstance(payload, list) else payload.get("value", payload)
        if not isinstance(rows, list):
            return []
        return [decode_document(row["document"]) for row in rows
                if isinstance(row, dict) and row.get("document")]

    def query_all(self, session, collection: str, page_size: int = 500,
                  max_pages: int = 12) -> list[dict]:
        """Page through a collection until it is exhausted.

        A single 500-row query returned exactly 500 open market orders, which means
        the result was truncated and any bid beyond the cutoff was invisible to the
        valuation engine.
        """
        collected: list[dict] = []
        for page in range(max_pages):
            batch = self.query_collection(
                session, collection, limit=page_size, offset=page * page_size)
            collected.extend(batch)
            if len(batch) < page_size:
                break
        return collected

    # --- callables -------------------------------------------------------
    def _call(self, session, name: str, data: dict | None = None) -> dict:
        return self.transport.call_function(name, data or {}, session.id_token)

    def finish_activity(self, session) -> dict:
        return self._call(session, "finishActivity")

    def buy_level(self, session, payload: dict) -> dict:
        """Free level-up. Payload comes from slcw.leveling.payload()."""
        return self._call(session, "buyLevel", payload)

    def claim_initial_reward(self, session, level: int) -> dict:
        return self._call(session, "claimInitialReward", {"level": level})

    def start_relax(self, session) -> dict:
        return self._call(session, "startRelax")

    def start_production(self, session, cycles: int = 1) -> dict:
        return self._call(session, "startProduction", {"cycles": cycles})

    def start_farming(self, session, payload: dict) -> dict:
        """Payload shape is built by slcw.farming.build_payload."""
        return self._call(session, "startFarming", payload)

    def start_refining(self, session, payload: dict) -> dict:
        """Payload shape is built by slcw.refining.Recipe.payload."""
        return self._call(session, "startRefining", payload)

    # --- hunt tasks, all argument-free ----------------------------------
    def get_task_status(self, session):
        from .tasks import parse_status
        return parse_status(self._call(session, "getTaskStatus"))

    def accept_task(self, session) -> dict:
        return self._call(session, "acceptTask")

    def start_task_battle(self, session) -> dict:
        return self._call(session, "startTaskBattle")

    def claim_task_reward(self, session) -> dict:
        return self._call(session, "claimTaskReward")

    def purchase_crafting_item(self, session, payload: dict) -> dict:
        """Buy refining catalysts for gold. Payload from refining.catalyst_payload."""
        return self._call(session, "purchaseCraftingItem", payload)

    def refill_energy_free(self, session) -> dict:
        """Free energy refill. Three per day; the server enforces the cap."""
        return self._call(session, "refillEnergyFree")

    def get_holdings(self, session) -> dict:
        """Item id -> total quantity across inventory slots."""
        from .market import inventory_holdings
        return inventory_holdings(self.get_inventory(session))

    def start_travel(self, session, destination_id: str) -> dict:
        return self._call(session, "startTravel", {"destinationId": destination_id})

    def start_battle(self, session, monster_id: str) -> dict:
        return self._call(session, "startBattle", {"monsterId": monster_id})

    def start_hunting(self, session, monster_id: str, monster_level: int,
                      mode: str, cycles: int = 0, hours: int = 0) -> dict:
        """Passive/idle combat against one monster — not part of the decision
        loop yet. From the bundle: cost is 3*cycles*3^(tier-1) gold plus
        3*cycles energy for energy mode (tier = ceil(monster_level/15)), or
        round(hours/8*4500) + 60*hours*3^(tier-1) gold for gold mode. Payoff
        has never been observed live."""
        return self._call(session, "startHunting", {
            "monsterId": monster_id, "monsterLevel": monster_level,
            "mode": mode, "cycles": cycles, "hours": hours})

    def process_turn(self, session, battle_id: str, attack: str, defense: str) -> dict:
        return self._call(session, "processTurn", {
            "battleId": battle_id, "attackZone": attack, "defenseZone": defense})

    def spend_attribute_points(self, session, target_type: str, target_id: str,
                               amount: int = 1) -> dict:
        return self._call(session, "spendAttributePoints", {
            "targetType": target_type, "targetId": target_id, "amount": amount})

    def open_chests(self, session, chest_template_id: str, quantity: int = 1) -> dict:
        return self._call(session, "openChests", {
            "chestTemplateId": chest_template_id, "quantity": quantity})

    def equip_item(self, session, instance_id: str) -> dict:
        return self._call(session, "equipItem", {"instanceId": instance_id})

    def unequip_item(self, session, slot_name: str) -> dict:
        return self._call(session, "unequipItem", {"slotName": slot_name})

    def complete_newbie_quest(self, session) -> dict:
        return self._call(session, "completeNewbieQuest")

    # --- clans -----------------------------------------------------------
    # Shapes measured live 2026-08-21; see slcw/clan.py for the full contract.
    def search_clans(self, session, query: str = "") -> dict:
        return self._call(session, "searchClans", {"query": query})

    def get_clan_members(self, session, clan_id: str) -> dict:
        return self._call(session, "getClanMembers", {"clanId": clan_id})

    def apply_clan(self, session, clan_id: str) -> dict:
        return self._call(session, "applyClan", {"clanId": clan_id})

    def cancel_clan_application(self, session, application_id: str) -> dict:
        return self._call(session, "cancelApplication",
                          {"applicationId": application_id})

    def leave_clan(self, session) -> dict:
        return self._call(session, "leaveClan")

    def make_donation(self, session, amount: int, currency: str = "gold") -> dict:
        """Treasury donation. One per wallet per UTC day, minimum 1,000 gold."""
        return self._call(session, "makeDonation",
                          {"amount": int(amount), "currency": currency})

    def submit_quest_resources(self, session, clan_id: str, quest_id: str,
                               item_id: str, amount: int) -> dict:
        """All four arguments are required — any subset returns INVALID_ARGUMENT."""
        return self._call(session, "submitQuestResources", {
            "clanId": clan_id, "questId": quest_id,
            "itemId": item_id, "amount": int(amount)})

    def get_clan(self, session, clan_id: str) -> dict:
        return self._document(f"clans/{clan_id}", session.id_token)

    def get_clan_member(self, session, clan_id: str, uid: str) -> dict:
        import urllib.parse as _up
        return self._document(
            f"clans/{clan_id}/members/{_up.quote(uid, safe='')}", session.id_token)

    def get_clan_quests(self, session, clan_id: str) -> list[dict]:
        """Quest documents, newest first, with their ids attached."""
        payload = self.transport.request(
            "GET", f"{FIRESTORE_BASE}/clans/{clan_id}/quests",
            headers={"Authorization": f"Bearer {session.id_token}"})
        out = []
        for doc in (payload or {}).get("documents") or []:
            parsed = decode_document(doc)
            parsed["questId"] = doc["name"].rsplit("/", 1)[-1]
            out.append(parsed)
        return out

    def onboard(self, session) -> dict:
        """Run idempotent initializers for a brand-new account.

        Each initializer may report that it already ran; that is a success, not a
        failure, so benign rejections are recorded rather than raised.
        """
        from .transport import ApiError

        results = {}
        for name in ("initializeImperialStats", "initializeNewInventory",
                     "migrateToNewInventory", "startFirstTravel"):
            try:
                results[name] = self._call(session, name)
            except ApiError as exc:
                results[name] = {"skipped": str(exc), "benign": exc.is_benign}
        return results
