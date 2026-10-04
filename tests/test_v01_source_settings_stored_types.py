"""Persisted source selection must remain SQLite TEXT at every read boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
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
