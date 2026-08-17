from __future__ import annotations
from enum import StrEnum

class TaskState(StrEnum):
    CREATED="CREATED"; READY="READY"; RUNNING="RUNNING"; WAITING_TOOL="WAITING_TOOL"; WAITING_APPROVAL="WAITING_APPROVAL"; PAUSED="PAUSED"; RETRYING="RETRYING"; BLOCKED="BLOCKED"; COMPLETED="COMPLETED"; FAILED="FAILED"; CANCELLED="CANCELLED"; ARCHIVED="ARCHIVED"

_ALLOWED = {
    TaskState.CREATED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.PAUSED, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset({TaskState.WAITING_TOOL, TaskState.WAITING_APPROVAL, TaskState.PAUSED, TaskState.RETRYING, TaskState.BLOCKED, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.WAITING_TOOL: frozenset({TaskState.RUNNING, TaskState.RETRYING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.WAITING_APPROVAL: frozenset({TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.PAUSED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.RETRYING: frozenset({TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset({TaskState.ARCHIVED}),
    TaskState.FAILED: frozenset({TaskState.READY, TaskState.ARCHIVED}),
    TaskState.CANCELLED: frozenset({TaskState.ARCHIVED}),
    TaskState.ARCHIVED: frozenset(),
}

def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in _ALLOWED[current]

def require_transition(current: TaskState, target: TaskState) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Invalid task transition: {current} -> {target}")
