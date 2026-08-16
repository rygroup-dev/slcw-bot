"""Hunt tasks: a free gold stream, gated behind character level 10.

Four callables, none of which take arguments:

    getTaskStatus()    -> {playerLevel, completedCount, allDone, hasActiveTask, task}
    acceptTask()       -> takes the next task in the ladder
    startTaskBattle()  -> {success, battleId}, then the normal turn loop
    claimTaskReward()  -> {goldAwarded, allTasksCompleted}

The active task also lives in Firestore at users/{uid}/activeTasks/current, with
status cycling active -> completed -> claimed. The client refuses to show any of
this below level 10, so the engine treats that as the gate too.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_LEVEL = 10


@dataclass
class Task:
    index: int = 0
    monster_id: str = ""
    monster_level: int = 1
    kills_required: int = 0
    kills_progress: int = 0
    status: str = ""
    gold_reward: int = 0

    @property
    def is_complete(self) -> bool:
        return self.status == "completed"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def kills_remaining(self) -> int:
        return max(0, self.kills_required - self.kills_progress)

    @property
    def gold_per_kill(self) -> float:
        return self.gold_reward / self.kills_required if self.kills_required else 0.0


@dataclass
class TaskStatus:
    player_level: int = 1
    completed_count: int = 0
    all_done: bool = False
    has_active_task: bool = False
    task: Task | None = None

    @property
    def eligible(self) -> bool:
        return self.player_level >= MIN_LEVEL and not self.all_done

    @property
    def can_accept(self) -> bool:
        return self.eligible and not self.has_active_task

    @property
    def can_claim(self) -> bool:
        return self.task is not None and self.task.is_complete

    @property
    def can_fight(self) -> bool:
        return self.task is not None and self.task.is_active


def parse_task(raw: dict | None) -> Task | None:
    if not isinstance(raw, dict) or not raw:
        return None
    return Task(
        index=int(raw.get("taskIndex", 0) or 0),
        monster_id=str(raw.get("monsterId") or ""),
        monster_level=int(raw.get("monsterLevel", 1) or 1),
        kills_required=int(raw.get("killsRequired", 0) or 0),
        kills_progress=int(raw.get("killsProgress", 0) or 0),
        status=str(raw.get("status") or ""),
        gold_reward=int(raw.get("goldReward", 0) or 0),
    )


def parse_status(payload: dict | None) -> TaskStatus:
    payload = payload or {}
    return TaskStatus(
        player_level=int(payload.get("playerLevel", 1) or 1),
        completed_count=int(payload.get("completedCount", 0) or 0),
        all_done=bool(payload.get("allDone")),
        has_active_task=bool(payload.get("hasActiveTask")),
        task=parse_task(payload.get("task")),
    )
