from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from nika_core.data.resource_durability_schema import RESOURCE_DURABILITY_SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability.legacy_database import LegacyDatabaseConflict, _inspect


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Ресурси зі пробілами.db")
    store.initialize()
    return store


def test_resource_stream_is_additive_and_uniquely_versioned(tmp_path) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as conn:
        assert [
            row["version"] for row in conn.execute(
                "SELECT version FROM resource_durability_schema_migrations ORDER BY version"
            )
        ] == [RESOURCE_DURABILITY_SCHEMA_VERSION]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'm3_extension_schema_migrations'"
        ).fetchone() is None
        assert {
            row["name"] for row in conn.execute("PRAGMA table_info(resource_budgets)")
        } == {
            "scope", "owner_id", "max_concurrent", "max_cpu_percent",
            "max_memory_percent", "updated_at",
        }


@pytest.mark.parametrize("fault", ["future", "gap", "missing_table", "missing_index"])
def test_resource_schema_fails_closed_on_corrupt_or_future_state(tmp_path, fault) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        if fault in {"future", "gap"}:
            if fault == "gap":
                conn.execute("DELETE FROM resource_durability_schema_migrations")
            conn.execute(
                "INSERT INTO resource_durability_schema_migrations VALUES (?, ?)",
                (2, datetime.now(UTC).isoformat()),
            )
        elif fault == "missing_table":
            conn.execute("DROP TABLE resource_requests")
        else:
            conn.execute("DROP INDEX idx_resource_requests_queue")
    with pytest.raises(RuntimeError):
        store.initialize()
    with pytest.raises(LegacyDatabaseConflict):
        _inspect(store.path)


def test_resource_marker_does_not_turn_empty_target_into_user_data(tmp_path) -> None:
    store = _store(tmp_path)
    state = _inspect(store.path)
    assert state is not None
    assert not state.has_data


def test_unmarked_resource_table_cannot_be_silently_adopted(tmp_path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute("DROP TABLE resource_durability_schema_migrations")
    with pytest.raises(RuntimeError, match="no migration authority"):
        store.initialize()


def test_failed_resource_migration_rolls_back_schema_and_marker(tmp_path, monkeypatch) -> None:
    from nika_core.data import resource_durability_schema as schema

    store = SQLiteStore(tmp_path / "atomic.db")
    monkeypatch.setitem(
        schema.RESOURCE_DURABILITY_MIGRATIONS,
        1,
        (
            "CREATE TABLE resource_requests (sequence INTEGER PRIMARY KEY)",
            "invalid migration",
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        store.initialize()
    with store.connection() as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'resource_requests'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'resource_durability_schema_migrations'"
        ).fetchone() is None
