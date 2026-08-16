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


@dataclass
class MonsterModel:
    """Per-monster estimates of which zones it blocks and which it attacks."""

    attacks_attempted: dict = field(default_factory=lambda: {z: 0 for z in ZONES})
    attacks_blocked: dict = field(default_factory=lambda: {z: 0 for z in ZONES})
    monster_attacks: dict = field(default_factory=lambda: {z: 0 for z in ZONES})
    rounds: int = 0

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


def select_monster(catalog: list[str] | None, player_level: int,
                   health_ratio: float) -> str | None:
    """Strongest monster that is still safe at the current health.

    Among equals the least dangerous one wins: same level, lower weapon power
    means fewer hit points lost per fight and so less rest time between them.
    """
    # None means "use the full registry"; an empty list means "nothing available"
    # and must not silently fall back to every monster in the game.
    source = known_monsters() if catalog is None else list(catalog)
    pool = [m for m in source if m in MONSTERS] or source
    if not pool:
        return None

    ceiling = player_level if health_ratio >= 0.8 else max(1, player_level - 2)
    eligible = [m for m in pool if monster_level(m) <= ceiling]
    if not eligible:
        # Nothing at or below the ceiling: take the weakest thing available
        # rather than refusing to fight at all.
        return min(pool, key=lambda m: (monster_level(m), monster_power(m)))

    return max(eligible, key=lambda m: (monster_level(m), -monster_power(m)))
