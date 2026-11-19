"""Controlled real-builder proof shared by source and frozen release gates."""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from nika_core.config import APP_CONFIG_SCHEMA_VERSION, AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.ui.bridge import UIActionBridge

BuildBridge = Callable[[AppConfig], tuple[UIActionBridge, Any]]


def _dispatch(bridge: UIActionBridge, action: str, payload: dict[str, Any]) -> None:
    result = bridge.dispatch(
        {"request_id": "controlled-resource-schedule-proof", "action_id": action, "payload": payload}
    )
    if result.get("status") != "completed":
        raise RuntimeError(f"Controlled packaged action failed: {action}")


def _state(bridge: UIActionBridge) -> Mapping[str, Any]:
    result = bridge.get_state()
    if result.get("ok") is not True or type(result.get("state")) is not dict:
        raise RuntimeError("Controlled packaged state is unavailable")
    return result["state"]


def _running_events(store: SQLiteStore, task_id: str) -> int:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND new_state = ?",
            (task_id, TaskState.RUNNING.value),
        ).fetchone()
    return int(row[0])


def _create_schedule(bridge: UIActionBridge, queue: TaskQueue, *, delay: int) -> str:
    prior = {task.task_id for task in queue.list_recent()}
    _dispatch(
        bridge,
        "schedule.create",
        {
            "command": "Порівняй два контрольовані джерела.",
            "delay_seconds": delay,
            "retry_interval_seconds": 1,
            "start_window_seconds": max(30, delay + 30),
        },
    )
    created = [task.task_id for task in queue.list_recent() if task.task_id not in prior]
    if len(created) != 1:
        raise RuntimeError("Controlled schedule did not create exactly one task")
    return created[0]


def run_resource_schedule_proof(build_bridge: BuildBridge) -> dict[str, object]:
    """Use only private fixtures and the actual production builder/runtime.

    No environment-owned or default database is opened, no UIA/NVDA claim is made,
    and no source contents, temporary paths or user state enter the output.
    """
    bridges: list[UIActionBridge] = []
    with tempfile.TemporaryDirectory(prefix="Nika — контрольований розклад ") as directory:
        root = Path(directory)
        sources = root / "Контрольовані джерела"
        sources.mkdir()
        a, b = sources / "Джерело А.txt", sources / "Джерело Б.txt"
        a.write_text("Контрольоване джерело А: спільна тема і перший приклад.\n", encoding="utf-8")
        b.write_text("Контрольоване джерело Б: спільна тема і другий приклад.\n", encoding="utf-8")
        config = AppConfig(
            schema_version=APP_CONFIG_SCHEMA_VERSION,
            database_path=root / "Дані з пробілами.db",
            v01_source_root=sources,
            v01_source_a=a,
            v01_source_b=b,
            resource_profile="normal",
            model_provider="mock",
            log_level="INFO",
        )
        try:
            first, _ = build_bridge(config)
            bridges.append(first)
            initial = _state(first)["resource_profile"]
            if initial != {"profile": "normal", "revision": 1}:
                raise RuntimeError("Controlled profile did not initialize once")
            _dispatch(first, "settings.resources.configure", {"profile": "economy", "revision": 1})
            first._close()
            restarted, _ = build_bridge(config)
            bridges.append(restarted)
            if _state(restarted)["resource_profile"] != {"profile": "economy", "revision": 2}:
                raise RuntimeError("Selected profile did not persist across builder restart")
            _dispatch(restarted, "settings.resources.configure", {"profile": "normal", "revision": 2})
            store = SQLiteStore(config.database_path)
            queue = TaskQueue(store)
            task_id = _create_schedule(restarted, queue, delay=1)
            deadline = time.monotonic() + 30
            while queue.get(task_id).state is not TaskState.COMPLETED:
                if queue.get(task_id).state in {TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED}:
                    raise RuntimeError("Controlled scheduled runtime did not complete")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Controlled scheduled runtime exceeded its proof deadline")
                threading.Event().wait(0.05)
            if _running_events(store, task_id) != 1:
                raise RuntimeError("Controlled task did not have exactly one durable start")
            _dispatch(restarted, "schedule.resume", {"task_id": task_id})
            threading.Event().wait(0.2)
            if _running_events(store, task_id) != 1:
                raise RuntimeError("Completed scheduled task was replayed")

            pending_id = _create_schedule(restarted, queue, delay=60)
            _dispatch(restarted, "schedule.pause", {"task_id": pending_id})
            states = {item["task_id"]: item["status"] for item in _state(restarted)["schedules"]}
            if states.get(pending_id) != "paused":
                raise RuntimeError("Controlled schedule pause was not durable")
            _dispatch(restarted, "schedule.resume", {"task_id": pending_id})
            _dispatch(restarted, "schedule.cancel", {"task_id": pending_id})
            if queue.get(pending_id).state is not TaskState.CANCELLED:
                raise RuntimeError("Controlled pending task was not cancelled")
            if _running_events(store, pending_id) != 0:
                raise RuntimeError("Cancelled pending task acquired runtime effects")
            return {
                "schema_version": 1,
                "fixture_kind": "controlled_temporary_sources",
                "actual_builder": True,
                "profile_persisted_after_restart": True,
                "scheduled_runtime_completed": True,
                "scheduled_running_event_count": 1,
                "completed_task_not_replayed": True,
                "pending_pause_resume_cancel": True,
                "cancelled_running_event_count": 0,
                "human_tested": False,
                "nvda_verified": False,
                "production_release_ready": False,
            }
        finally:
            for bridge in reversed(bridges):
                bridge._close()
