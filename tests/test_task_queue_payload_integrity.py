"""Malformed durable tasks must never be projected as executable commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskPayloadCorruptionError, TaskQueue
from nika_core.v01_source_settings import V01SourceSettings


def _queue(tmp_path: Path) -> tuple[SQLiteStore, TaskQueue]:
    store = SQLiteStore(tmp_path / "Дані з пробілами" / "nika.db")
    store.initialize()
    return store, TaskQueue(store)


@pytest.mark.parametrize(
    "persisted",
    [
        pytest.param("[]", id="array"),
        pytest.param("null", id="null"),
        pytest.param('"not-a-command"', id="scalar-string"),
        pytest.param('{"command":', id="broken-json"),
        pytest.param('{"command":"first","command":"second"}', id="duplicate"),
        pytest.param('{"metadata":{"x":1,"x":2}}', id="nested-duplicate"),
        pytest.param('{"score":NaN}', id="nan"),
        pytest.param('{"score":Infinity}', id="positive-infinity"),
        pytest.param('{"score":-Infinity}', id="negative-infinity"),
        pytest.param('{"score":1e999}', id="float-overflow"),
        pytest.param(sqlite3.Binary(b'{"command":"test"}'), id="sqlite-blob"),
        pytest.param(123, id="sqlite-numeric"),
    ],
)
def test_corrupt_payload_fails_closed_on_get_and_list(
    tmp_path: Path, persisted: object
) -> None:
    store, queue = _queue(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Порівняй джерела"},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (persisted, task.task_id),
        )
    with pytest.raises(TaskPayloadCorruptionError, match="пошкоджені"):
        queue.get(task.task_id)
    with pytest.raises(TaskPayloadCorruptionError, match="пошкоджені"):
        queue.list_recent()
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id = ?", (task.task_id,)
        ).fetchone()[0] == 1


def test_valid_unicode_command_and_missing_identity_unchanged(tmp_path: Path) -> None:
    _, queue = _queue(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Опрацюй джерела", "metadata": {"мова": "українська"}},
    )
    assert queue.get(task.task_id) == task
    assert queue.list_recent() == (task,)
    with pytest.raises(KeyError):
        queue.get("task-that-does-not-exist")


def test_corruption_cannot_bind_current_sources_as_legacy_fallback(
    tmp_path: Path,
) -> None:
    store, queue = _queue(tmp_path)
    config = AppConfig(database_path=store.path)
    settings = V01SourceSettings(store, config)
    root = tmp_path / "Джерела з пробілами"
    root.mkdir()
    (root / "перший.txt").write_text("Перший", encoding="utf-8")
    (root / "другий.txt").write_text("Другий", encoding="utf-8")
    assert settings.configure({
        "root": str(root),
        "source_a": "перший.txt",
        "source_b": "другий.txt",
        "revision": 0,
    }).status == "completed"
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Порівняй"}),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = '[]' WHERE task_id = ?",
            (task.task_id,),
        )
    with pytest.raises(TaskPayloadCorruptionError, match="пошкоджені"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_create_rejects_nonfinite_payload_before_durable_write(
    tmp_path: Path, value: float
) -> None:
    store, queue = _queue(tmp_path)
    with pytest.raises(ValueError):
        queue.create(
            workspace_id="default",
            agent_id="nika.default",
            payload={"command": "Порівняй", "score": value},
        )
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
