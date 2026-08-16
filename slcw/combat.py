"""Combat strategy learned from observed turn results.

Each `processTurn` response exposes both sides of the exchange:

    player:  {"zone": "legs",  "damage": 2, "type": "blocked"}
    monster: {"zone": "head",  "damage": 2, "type": "hit"}

`player.zone` is the zone we attacked and `player.type == "blocked"` means the
monster guarded it. `monster.zone` is the zone the monster attacked, and a type of
"hit" or "crit" means our defense guessed wrong. Both signals are per-monster and
stable enough to learn, so zone choice becomes an estimate instead of the coin flip
that `random.choice` was doing.
"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA
from .monster_data import MONSTERS

ZONES = ("head", "torso", "legs")
MEMORY_PATH = DATA / "combat_memory.json"

# Chance of ignoring the model and picking at random. Keeps the estimates honest if
# the monster's behaviour shifts, and stops our zone choices from becoming perfectly
# predictable themselves.
EXPLORATION_RATE = 0.18

# Laplace smoothing, so a single observation cannot drive the model to certainty.
PRIOR = 1.0

# Expected damage must stay this far below current health before a fight is
# accepted. The damage formula is not published, so the estimate is coarse and
# the margin absorbs that.
SURVIVAL_MARGIN = 0.6

# How often to fight something never fought before. Drop tables are server-side,
# so a monster's worth is unknown until it has been tried at least once.
DISCOVERY_RATE = 0.15


@dataclass
class MonsterModel:
    """What fighting this monster has actually cost and returned.

    Drop tables are server-side and appear nowhere in the frontend, so what a
    monster is worth cannot be looked up — only measured. Every finished battle
    adds to these counters, and monster choice is made from them rather than
    from an invented damage formula.
    """

    attacks_attempted: dict = field(default_factory=lambda: {z: 0 for z in ZONES})
    attacks_blocked: dict = field(default_factory=lambda: {z: 0 for z in ZONES})
    monster_attacks: dict = field(default_factory=lambda: {z: 0 for z in ZONES})
    rounds: int = 0

    # --- outcomes, filled in when a battle settles -----------------------
    battles: int = 0
    wins: int = 0
    total_turns: int = 0
    total_damage_taken: int = 0
    total_xp: int = 0
    total_gold: int = 0
    drops: dict = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.battles if self.battles else 0.0

    @property
    def avg_turns(self) -> float:
        return self.total_turns / self.battles if self.battles else 0.0

    @property
    def avg_damage(self) -> float:
        return self.total_damage_taken / self.battles if self.battles else 0.0

    @property
    def avg_xp(self) -> float:
        return self.total_xp / self.battles if self.battles else 0.0

    def avg_drops(self) -> dict:
        """Expected items per battle, from what actually dropped."""
        if not self.battles:
            return {}
        return {item: count / self.battles for item, count in self.drops.items()}

    def record_battle(self, reward: dict, turns: int, damage_taken: int) -> None:
        """Fold one settled battle into the running totals."""
        self.battles += 1
        self.total_turns += max(0, int(turns))
        self.total_damage_taken += max(0, int(damage_taken))

        reward = reward or {}
        if reward.get("winner") == "player":
            self.wins += 1
        self.total_xp += int(reward.get("xp", 0) or 0)
        self.total_gold += int(reward.get("gold", 0) or 0)
        for item in reward.get("items") or []:
            key = item.get("id")
            if key:
                self.drops[key] = self.drops.get(key, 0) + int(item.get("quantity", 0) or 0)

    def block_rate(self, zone: str) -> float:
        attempted = self.attacks_attempted.get(zone, 0)
        blocked = self.attacks_blocked.get(zone, 0)
        return (blocked + PRIOR) / (attempted + 2 * PRIOR)

    def attack_rate(self, zone: str) -> float:
        total = sum(self.monster_attacks.values())
        return (self.monster_attacks.get(zone, 0) + PRIOR) / (total + len(ZONES) * PRIOR)

    def best_attack_zone(self) -> str:
        """Attack where the monster blocks least."""
        return min(ZONES, key=self.block_rate)

    def best_defense_zone(self) -> str:
        """Defend where the monster attacks most."""
        return max(ZONES, key=self.attack_rate)

    def observe(self, turn: dict) -> None:
        player = turn.get("player") or {}
        monster = turn.get("monster") or {}

        attacked = player.get("zone")
        if attacked in ZONES:
            self.attacks_attempted[attacked] += 1
            if player.get("type") == "blocked":
                self.attacks_blocked[attacked] += 1

        incoming = monster.get("zone")
        if incoming in ZONES:
            self.monster_attacks[incoming] += 1

        self.rounds += 1

    def to_dict(self) -> dict:
        return {
            "attacks_attempted": self.attacks_attempted,
            "attacks_blocked": self.attacks_blocked,
            "monster_attacks": self.monster_attacks,
            "rounds": self.rounds,
            "battles": self.battles,
            "wins": self.wins,
            "total_turns": self.total_turns,
            "total_damage_taken": self.total_damage_taken,
            "total_xp": self.total_xp,
            "total_gold": self.total_gold,
            "drops": self.drops,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MonsterModel":
        model = cls()
        for key in ("attacks_attempted", "attacks_blocked", "monster_attacks"):
            stored = payload.get(key) or {}
            target = getattr(model, key)
            for zone in ZONES:
                target[zone] = int(stored.get(zone, 0))
        model.rounds = int(payload.get("rounds", 0))
        for key in ("battles", "wins", "total_turns", "total_damage_taken",
                    "total_xp", "total_gold"):
            setattr(model, key, int(payload.get(key, 0) or 0))
        model.drops = {k: int(v) for k, v in (payload.get("drops") or {}).items()}
        return model


class CombatMemory:
    """Persistent per-monster models, keyed by monster template id."""

    def __init__(self, path: Path = MEMORY_PATH):
        self.path = path
        self.models: dict = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return
        self.models = {k: MonsterModel.from_dict(v) for k, v in payload.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {k: v.to_dict() for k, v in self.models.items()}, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def model_for(self, monster_id: str) -> MonsterModel:
        return self.models.setdefault(monster_id, MonsterModel())

    def choose_zones(self, monster_id: str, rng: random.Random | None = None) -> tuple[str, str]:
        rng = rng or random
        model = self.model_for(monster_id)
        attack = (rng.choice(ZONES) if rng.random() < EXPLORATION_RATE
                  else model.best_attack_zone())
        defense = (rng.choice(ZONES) if rng.random() < EXPLORATION_RATE
                   else model.best_defense_zone())
        return attack, defense

    def observe(self, monster_id: str, turn: dict) -> None:
        self.model_for(monster_id).observe(turn)

    def record_battle(self, monster_id: str, reward: dict, turns: int,
                      damage_taken: int) -> None:
        self.model_for(monster_id).record_battle(reward, turns, damage_taken)


def monster_level(monster_id: str) -> int:
    """Level of a monster, from the registry when known, else parsed from its id."""
    entry = MONSTERS.get(monster_id)
    if entry:
        return entry[1]
    for chunk in monster_id.split("_"):
        if chunk.startswith("lvl"):
            try:
                return int(chunk[3:])
            except ValueError:
                return 1
    return 1


def monster_power(monster_id: str) -> int:
    entry = MONSTERS.get(monster_id)
    return entry[2] if entry else 0


def monster_health(monster_id: str) -> int:
    entry = MONSTERS.get(monster_id)
    return entry[5] if entry else 0


def monster_name(monster_id: str) -> str:
    entry = MONSTERS.get(monster_id)
    return entry[0] if entry else monster_id


def known_monsters(max_level: int | None = None) -> list[str]:
    """Every real monster id, optionally capped by level.

    Ids are taken from the registry rather than written by hand: `startBattle`
    rejects anything that does not exist, and the previous hardcoded list
    contained `forestspider_lvl1_1`, which the game has never had.
    """
    ids = MONSTERS.keys()
    if max_level is not None:
        ids = [m for m in ids if MONSTERS[m][1] <= max_level]
    return sorted(ids, key=lambda m: (MONSTERS[m][1], m))


def survivable(monster_id: str, weapon_power: float, physical_defense: float,
               current_health: int) -> bool:
    """Whether the fight is winnable without dropping to zero.

    Deliberately crude, because the exact damage formula is not published: a
    monster is refused when our attack cannot out-pace its health pool, or when
    its attack out-paces our defence badly enough that a long fight would run us
    out of hit points. Observed results override this as soon as any exist.
    """
    entry = MONSTERS.get(monster_id)
    if entry is None:
        return True
    _, _, mon_power, mon_defense, _, mon_health = entry

    # Effective damage per side, floored at 1 so nothing is unkillable on paper.
    our_damage = max(1.0, weapon_power - mon_defense * 0.5)
    their_damage = max(1.0, mon_power - physical_defense * 0.25)

    turns = mon_health / our_damage
    expected_loss = turns * their_damage
    return expected_loss < current_health * SURVIVAL_MARGIN


def expected_value(monster_id: str, memory: "CombatMemory | None",
                   market=None, xp_gold: float = 8.0) -> float:
    """Gold-equivalent per battle, from what this monster has actually given.

    Returns 0 for a monster never fought, which is what keeps exploration
    deliberate rather than accidental.
    """
    if memory is None or monster_id not in memory.models:
        return 0.0
    model = memory.models[monster_id]
    if not model.battles:
        return 0.0

    value = model.avg_xp * xp_gold + (model.total_gold / model.battles)
    if market is not None:
        for item, per_battle in model.avg_drops().items():
            bid = market.best_bid(item) or 0.0
            value += bid * per_battle
    return value * max(model.win_rate, 0.1)


def select_monster(catalog: list[str] | None, player_level: int,
                   health_ratio: float, weapon_power: float | None = None,
                   physical_defense: float | None = None,
                   current_health: int | None = None,
                   memory: "CombatMemory | None" = None, market=None,
                   rng: random.Random | None = None) -> str | None:
    """Pick the most rewarding monster we can actually beat.

    Level is a gate, not the goal. Within what is survivable at our real combat
    stats, the choice is made on measured returns — XP, gold and drops priced at
    live bids — because drop tables are server-side and cannot be read anywhere.
    Unfought monsters are tried occasionally so the measurements can exist at
    all; without that the bot would farm its first monster forever.
    """
    rng = rng or random
    source = known_monsters() if catalog is None else list(catalog)
    pool = [m for m in source if m in MONSTERS] or source
    if not pool:
        return None

    ceiling = player_level if health_ratio >= 0.8 else max(1, player_level - 2)
    eligible = [m for m in pool if monster_level(m) <= ceiling]
    if not eligible:
        return min(pool, key=lambda m: (monster_level(m), monster_power(m)))

    # Drop anything our stats say we would lose, when stats are available.
    if None not in (weapon_power, physical_defense, current_health):
        safe = [m for m in eligible
                if survivable(m, weapon_power, physical_defense, current_health)]
        eligible = safe or eligible

    measured = [m for m in eligible
                if memory and memory.models.get(m) and memory.models[m].battles]
    untried = [m for m in eligible if m not in measured]

    # Try something new now and then, and always when nothing is measured yet.
    if untried and (not measured or rng.random() < DISCOVERY_RATE):
        return max(untried, key=monster_level)

    if measured:
        return max(measured, key=lambda m: expected_value(m, memory, market))

    return max(eligible, key=lambda m: (monster_level(m), -monster_power(m)))
