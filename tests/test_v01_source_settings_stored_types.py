"""Persisted source selection must remain SQLite TEXT at every read boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
from nika_core.v01_source_settings import (
    SourceSelection,
    SourceSetupError,
    V01SourceSettings,
)


def _configured(tmp_path: Path) -> tuple[SQLiteStore, V01SourceSettings]:
    config = AppConfig(database_path=tmp_path / "nika.db")
    store = SQLiteStore(config.database_path)
    store.initialize()
    settings = V01SourceSettings(store, config)
    root = tmp_path / "Джерела з пробілами"
    root.mkdir()
    (root / "один.txt").write_text("Перший", encoding="utf-8")
    (root / "два.txt").write_text("Другий", encoding="utf-8")
    assert settings.configure(
        {
            "root": str(root),
            "source_a": "один.txt",
            "source_b": "два.txt",
            "revision": 0,
        }
    ).status == "completed"
    return store, settings


def test_stored_selection_accepts_text_but_rejects_identical_json_blob(
    tmp_path: Path,
) -> None:
    _, settings = _configured(tmp_path)
    selection = settings.snapshot()
    serialized = SourceSelection(
        root=selection["root"],
        source_a=selection["source_a"],
        source_b=selection["source_b"],
    ).model_dump_json()
    assert SourceSelection.from_stored(serialized).root == selection["root"]
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        SourceSelection.from_stored(serialized.encode("utf-8"))  # type: ignore[arg-type]


def test_blob_settings_fail_closed_without_replacing_selection(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    original_selection = settings.snapshot()
    with store.connection() as conn:
        original = conn.execute(
            "SELECT selection_json FROM v01_source_settings WHERE singleton = 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE v01_source_settings SET selection_json = ? WHERE singleton = 1",
            (sqlite3.Binary(original.encode("utf-8")),),
        )
    assert settings.snapshot() == {"status": "invalid"}
    assert settings.configure(
        {
            "root": original_selection["root"],
            "source_a": original_selection["source_a"],
            "source_b": original_selection["source_b"],
            "revision": 1,
        }
    ).status == "rejected"
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        settings.prepare_task_payload({"command": "Порівняй"})
    with store.connection() as conn:
        assert type(conn.execute(
            "SELECT selection_json FROM v01_source_settings WHERE singleton = 1"
        ).fetchone()[0]) is bytes


def test_blob_pinned_selection_never_escapes_as_attribute_error(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    selection_id = settings.prepare_task_payload({"command": "Порівняй"})[
        "v01_source_selection"
    ]
    with store.connection() as conn:
        original = conn.execute(
            "SELECT selection_json FROM v01_source_selections WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE v01_source_selections SET selection_json = ? WHERE selection_id = ?",
            (sqlite3.Binary(original.encode("utf-8")), selection_id),
        )
        with pytest.raises(SourceSetupError, match="не вдалося перевірити"):
            settings._selection_by_id(conn, selection_id)


def test_blob_task_binding_fails_closed_for_legacy_task(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    with store.connection() as conn:
        original = conn.execute(
            "SELECT selection_json FROM v01_source_settings WHERE singleton = 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO v01_task_source_bindings VALUES (?, ?, ?)",
            ("legacy-task", sqlite3.Binary(original.encode("utf-8")), "2026-10-04"),
        )
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        settings.for_task("legacy-task")

def test_unknown_task_cannot_create_source_binding(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    with pytest.raises(SourceSetupError, match="не знайдено"):
        settings.for_task("nonexistent-task")
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            ("nonexistent-task",),
        ).fetchone()[0] == 0


def test_stale_task_read_cannot_create_orphan_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, settings = _configured(tmp_path)

    def stale_get(_queue: TaskQueue, task_id: str) -> TaskRecord:
        return TaskRecord(
            task_id=task_id,
            workspace_id="default",
            agent_id="nika.default",
            state=TaskState.CREATED,
            payload={"command": "Порівняй"},
        )

    monkeypatch.setattr(TaskQueue, "get", stale_get)
    with pytest.raises(SourceSetupError, match="не знайдено"):
        settings.for_task("deleted-task")
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            ("deleted-task",),
        ).fetchone()[0] == 0


def test_valid_task_keeps_source_binding(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Порівняй"}),
    )
    selection = settings.for_task(task.task_id)
    assert selection.source_a != selection.source_b
    assert settings.for_task(task.task_id) == selection
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 1


def test_existing_legacy_binding_without_queue_row_remains_readable(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    snapshot = settings.snapshot()
    selection = SourceSelection(
        root=snapshot["root"],
        source_a=snapshot["source_a"],
        source_b=snapshot["source_b"],
    )
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_task_source_bindings VALUES (?, ?, ?)",
            ("legacy-task", selection.model_dump_json(), "2026-10-04"),
        )
    assert settings.for_task("legacy-task") == selection


def test_task_get_is_write_fenced_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, settings = _configured(tmp_path)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Порівняй"}),
    )
    original_get = TaskQueue.get
    observed = []

    def read_while_other_writer_competes(queue: TaskQueue, task_id: str) -> TaskRecord:
        record = original_get(queue, task_id)
        # A second SQLite connection must be unable to delete/reuse the task
        # between the canonical task read and the source-binding write.
        with (
            sqlite3.connect(store.path, timeout=0) as competing,
            pytest.raises(sqlite3.OperationalError),
        ):
            competing.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        observed.append(task_id)
        return record

    monkeypatch.setattr(TaskQueue, "get", read_while_other_writer_competes)
    assert settings.for_task(task.task_id).source_a != ""
    assert observed == [task.task_id]
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 1
