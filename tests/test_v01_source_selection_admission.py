from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_source_settings import (
    SourceSelection,
    SourceSetupError,
    V01SourceSettings,
)
from scripts.nika_windows import build_windows_bridge

_CANARY = "PRIVATE_SOURCE_CORRUPTION_CANARY"
_TOO_LARGE = "x" * (512 * 1024 + 1)


def _settings(tmp_path: Path) -> tuple[SQLiteStore, AppConfig, V01SourceSettings]:
    config = AppConfig(database_path=tmp_path / "Дані" / "ніка.db")
    store = SQLiteStore(config.database_path)
    store.initialize()
    settings = V01SourceSettings(store, config)
    root = tmp_path / "Приватні джерела"
    root.mkdir()
    (root / "а.txt").write_text("Перше свідчення", encoding="utf-8")
    (root / "б.txt").write_text("Друге свідчення", encoding="utf-8")
    assert settings.configure(
        {"root": str(root), "source_a": "а.txt", "source_b": "б.txt", "revision": 0}
    ).status == "completed"
    return store, config, settings


@pytest.mark.parametrize(
    "stored",
    [
        sqlite3.Binary(b'{"schema_version":1,"root":"C:/PRIVATE_SOURCE_CORRUPTION_CANARY"}'),
        _CANARY,
        _TOO_LARGE,
        "ї" * (280 * 1024),
    ],
)
def test_malformed_stored_selection_returns_user_safe_error(stored: object) -> None:
    with pytest.raises(SourceSetupError) as failure:
        SourceSelection.from_stored(stored)  # type: ignore[arg-type]
    assert _CANARY not in str(failure.value)


@pytest.mark.parametrize("storage", ["blob", "oversized"])
def test_corrupt_global_selection_blocks_task_and_preserves_row(
    tmp_path: Path, storage: str
) -> None:
    store, _, settings = _settings(tmp_path)
    with store.connection() as conn:
        body = conn.execute(
            "SELECT selection_json FROM v01_source_settings WHERE singleton = 1"
        ).fetchone()[0]
        damaged = sqlite3.Binary(body.encode("utf-8")) if storage == "blob" else _TOO_LARGE
        conn.execute(
            "UPDATE v01_source_settings SET selection_json = ? WHERE singleton = 1",
            (damaged,),
        )
    assert settings.snapshot() == {"status": "invalid"}
    assert settings.configure(
        {**json.loads(body), "revision": 1}
    ).status == "rejected"
    with pytest.raises(SourceSetupError) as failure:
        settings.prepare_task_payload({"command": "Порівняй"})
    assert _CANARY not in str(failure.value)
    assert TaskQueue(store).list_recent() == ()
    with store.connection() as conn:
        row = conn.execute(
            "SELECT selection_json, revision FROM v01_source_settings WHERE singleton = 1"
        ).fetchone()
    assert row["revision"] == 1
    assert row["selection_json"] == damaged


@pytest.mark.parametrize("storage", ["blob", "oversized"])
def test_corrupt_content_addressed_selection_blocks_binding_without_rewrite(
    tmp_path: Path, storage: str
) -> None:
    store, config, settings = _settings(tmp_path)
    build_windows_bridge(config)  # Register the canonical packaged workspace and agent.
    payload = settings.prepare_task_payload({"command": "Порівняй"})
    task = TaskQueue(store).create(
        workspace_id="default", agent_id="nika.default", payload=payload
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT selection_json FROM v01_source_selections WHERE selection_id = ?",
            (payload["v01_source_selection"],),
        ).fetchone()
        body = row["selection_json"]
        damaged = sqlite3.Binary(body.encode("utf-8")) if storage == "blob" else _TOO_LARGE
        conn.execute(
            "UPDATE v01_source_selections SET selection_json = ? WHERE selection_id = ?",
            (damaged, payload["v01_source_selection"]),
        )
        before_audit = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with pytest.raises(SourceSetupError) as failure:
        settings.for_task(task.task_id)
    assert _CANARY not in str(failure.value)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before_audit
        assert conn.execute(
            "SELECT selection_json FROM v01_source_selections WHERE selection_id = ?",
            (payload["v01_source_selection"],),
        ).fetchone()[0] == damaged


@pytest.mark.parametrize("storage", ["blob", "oversized"])
def test_corrupt_existing_task_binding_is_not_silently_replaced(
    tmp_path: Path, storage: str
) -> None:
    store, config, settings = _settings(tmp_path)
    build_windows_bridge(config)
    payload = settings.prepare_task_payload({"command": "Порівняй"})
    task = TaskQueue(store).create(
        workspace_id="default", agent_id="nika.default", payload=payload
    )
    assert settings.for_task(task.task_id).source_a.endswith("а.txt")
    with store.connection() as conn:
        body = conn.execute(
            "SELECT selection_json FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0]
        damaged = sqlite3.Binary(body.encode("utf-8")) if storage == "blob" else _TOO_LARGE
        conn.execute(
            "UPDATE v01_task_source_bindings SET selection_json = ? WHERE task_id = ?",
            (damaged, task.task_id),
        )
        before_audit = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with pytest.raises(SourceSetupError) as failure:
        settings.for_task(task.task_id)
    assert _CANARY not in str(failure.value)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT selection_json FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == damaged
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before_audit


def test_valid_selection_reopens_and_keeps_original_task_binding(tmp_path: Path) -> None:
    store, config, settings = _settings(tmp_path)
    build_windows_bridge(config)
    expected = settings.snapshot()
    assert expected["status"] == "ready"
    payload = settings.prepare_task_payload({"command": "Порівняй"})
    task = TaskQueue(store).create(
        workspace_id="default", agent_id="nika.default", payload=payload
    )
    reopened = V01SourceSettings(SQLiteStore(config.database_path), config)
    assert reopened.snapshot() == expected
    selection = reopened.for_task(task.task_id)
    assert selection.root == expected["root"]
    assert selection.source_a == expected["source_a"]
    assert selection.source_b == expected["source_b"]
    assert reopened.for_task(task.task_id) == selection
