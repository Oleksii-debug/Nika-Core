"""SQLite BLOB corruption must not be silently accepted as model settings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_model_settings import (
    ModelSelection,
    ModelSetupError,
    V01ModelSettings,
)


def _configured(tmp_path: Path) -> tuple[SQLiteStore, V01ModelSettings]:
    store = SQLiteStore(tmp_path / "Дані програми" / "ніка.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure({"route_kind": "deterministic", "revision": 0}).status == (
        "completed"
    )
    return store, settings


def test_model_json_roundtrips_only_as_text() -> None:
    body = ModelSelection(route_kind="deterministic").canonical_json()
    assert ModelSelection.from_stored(body).route_kind == "deterministic"
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        ModelSelection.from_stored(body.encode("utf-8"))  # type: ignore[arg-type]


def test_model_settings_blob_is_invalid_and_never_replaced(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    with store.connection() as conn:
        body = conn.execute(
            "SELECT selection_json FROM v01_model_settings WHERE singleton = 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE v01_model_settings SET selection_json = ? WHERE singleton = 1",
            (sqlite3.Binary(body.encode("utf-8")),),
        )
    assert settings.snapshot() == {"status": "invalid"}
    assert settings.configure({"route_kind": "deterministic", "revision": 1}).status == (
        "rejected"
    )
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        settings.prepare_task_payload({"command": "Порівняй"})
    with store.connection() as conn:
        stored = conn.execute(
            "SELECT selection_json FROM v01_model_settings WHERE singleton = 1"
        ).fetchone()[0]
        assert type(stored) is bytes


def test_model_selection_blob_does_not_create_task_binding(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Порівняй"}),
    )
    selection_id = task.payload["v01_model_selection"]
    with store.connection() as conn:
        body = conn.execute(
            "SELECT selection_json FROM v01_model_selections WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE v01_model_selections SET selection_json = ? WHERE selection_id = ?",
            (sqlite3.Binary(body.encode("utf-8")), selection_id),
        )
    with pytest.raises(ModelSetupError, match="не знайдено"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_model_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0


def test_model_task_binding_blob_is_rejected_after_restart(tmp_path: Path) -> None:
    store, settings = _configured(tmp_path)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Порівняй"}),
    )
    assert settings.for_task(task.task_id).route_kind == "deterministic"
    with store.connection() as conn:
        body = conn.execute(
            "SELECT selection_json FROM v01_task_model_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE v01_task_model_bindings SET selection_json = ? WHERE task_id = ?",
            (sqlite3.Binary(body.encode("utf-8")), task.task_id),
        )
    restarted = V01ModelSettings(SQLiteStore(store.path))
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        restarted.for_task(task.task_id)
