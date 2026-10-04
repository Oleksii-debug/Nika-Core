"""Corrupt source/model migration history must fail before any implicit repair."""

from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings
from nika_core.v01_source_settings import SourceSetupError, V01SourceSettings


def _open(kind: str, store: SQLiteStore, config: AppConfig) -> None:
    if kind == "source":
        V01SourceSettings(store, config)
    else:
        V01ModelSettings(store)


@pytest.mark.parametrize("kind", ["source", "model"])
def test_valid_migration_history_allows_restart(kind: str, tmp_path: Path) -> None:
    config = AppConfig(database_path=tmp_path / "Дані програми" / "ніка.db")
    store = SQLiteStore(config.database_path)
    store.initialize()
    _open(kind, store, config)
    _open(kind, SQLiteStore(config.database_path), config)


@pytest.mark.parametrize("kind", ["source", "model"])
@pytest.mark.parametrize(
    "history",
    [
        (-1,),
        (0,),
        (-1, 1),
        (0, 1),
    ],
)
def test_nonpositive_migration_history_is_rejected_without_rewrite(
    kind: str, history: tuple[int, ...], tmp_path: Path
) -> None:
    config = AppConfig(database_path=tmp_path / "Дані програми" / "ніка.db")
    store = SQLiteStore(config.database_path)
    store.initialize()
    _open(kind, store, config)
    table = f"v01_{kind}_settings_schema"
    with store.connection() as conn:
        conn.execute(f"DELETE FROM {table}")
        for version in history:
            conn.execute(
                f"INSERT INTO {table}(version, applied_at) VALUES (?, ?)",
                (version, "2026-10-04"),
            )

    error = SourceSetupError if kind == "source" else ModelSetupError
    with pytest.raises(error, match="некоректна"):
        _open(kind, SQLiteStore(config.database_path), config)

    with store.connection() as conn:
        rows = conn.execute(f"SELECT version FROM {table} ORDER BY version").fetchall()
    assert [row[0] for row in rows] == sorted(history)
