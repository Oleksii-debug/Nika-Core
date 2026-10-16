from __future__ import annotations

import sqlite3

import pytest

from nika_core.data.experience_ledger_schema import EXPERIENCE_LEDGER_SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.experience_ledger import ExperienceLedger, ExperienceLedgerUnavailableError


def test_ledger_constructor_is_side_effect_free_and_uninitialized_use_fails_closed(
    tmp_path,
) -> None:
    path = tmp_path / "Користувач Ніка" / "ledger.db"
    ledger = ExperienceLedger(SQLiteStore(path))

    assert not path.exists()
    with pytest.raises(ExperienceLedgerUnavailableError, match="initialize SQLiteStore"):
        ledger.get("task-1:recovery:1")


def test_canonical_store_initialization_owns_experience_ledger_schema(tmp_path) -> None:
    path = tmp_path / "дані з пробілом" / "nika.db"
    store = SQLiteStore(path)
    store.initialize()

    with store.connection() as conn:
        version = conn.execute(
            "SELECT MAX(version) AS version FROM experience_ledger_schema_migrations"
        ).fetchone()["version"]
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'continuity_experience_events'"
        ).fetchone()
        index = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_continuity_experience_task_time'"
        ).fetchone()

    assert version == EXPERIENCE_LEDGER_SCHEMA_VERSION
    assert table["name"] == "continuity_experience_events"
    assert index["name"] == "idx_continuity_experience_task_time"


def test_newer_experience_ledger_schema_fails_closed_on_restart_and_use(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    newer = EXPERIENCE_LEDGER_SCHEMA_VERSION + 1

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO experience_ledger_schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (newer, "2026-09-12T00:00:00+00:00"),
        )

    with pytest.raises(RuntimeError, match="experience ledger database schema"):
        SQLiteStore(path).initialize()

    ledger = ExperienceLedger(SQLiteStore(path))
    with pytest.raises(ExperienceLedgerUnavailableError, match="does not match supported schema"):
        ledger.get("task-2:recovery:1")


def test_constructor_does_not_repair_missing_durable_table(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute("DROP TABLE continuity_experience_events")

    ledger = ExperienceLedger(SQLiteStore(path))
    with store.connection() as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'continuity_experience_events'"
        ).fetchone()
    assert table is None

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        ledger.get("task-3:recovery:1")
