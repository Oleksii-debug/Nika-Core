from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_m3_extension_migration_history_hole_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute("DELETE FROM m3_extension_schema_migrations WHERE version = 1")
        rows = conn.execute(
            "SELECT version FROM m3_extension_schema_migrations ORDER BY version"
        ).fetchall()
        assert [int(item["version"]) for item in rows] == [2]

    with pytest.raises(RuntimeError, match="migration history is non-contiguous"):
        store.initialize()


def test_m3_current_schema_marker_cannot_mask_missing_table(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute("DROP TABLE scheduled_job_bindings")
        row = conn.execute(
            "SELECT MAX(version) AS version FROM m3_extension_schema_migrations"
        ).fetchone()
        assert int(row["version"]) == 2

    with pytest.raises(RuntimeError, match="schema table missing: scheduled_job_bindings"):
        store.initialize()


def test_m3_orphaned_extension_state_without_prerequisites_fails_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute("DROP TABLE scheduled_job_bindings")
        conn.execute("DROP TABLE resource_requests")
        conn.execute("DROP TABLE scheduled_jobs")
        conn.execute("DROP TABLE resource_budgets")

    with pytest.raises(RuntimeError, match="extension state exists without prerequisite tables"):
        store.initialize()


def test_m3_future_version_still_precedes_shape_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute("DROP TABLE resource_requests")
        conn.execute(
            "INSERT INTO m3_extension_schema_migrations(version, applied_at) VALUES (?, ?)",
            (3, datetime.now(UTC).isoformat()),
        )

    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()
