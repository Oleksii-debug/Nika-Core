from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.diagnostics import HealthService, HealthStatus
from nika_core.resources.contracts import ResourceSnapshot

_FIXED_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


class _StaticObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(10.0, 20.0, 1024)


def _run(database: Path):
    return HealthService(
        AppConfig(database_path=database, model_provider="mock"),
        resource_observer=_StaticObserver(),
        clock=lambda: _FIXED_NOW,
    ).run()


def _check_map(report) -> dict[str, HealthStatus]:
    return {check.check_id: check.status for check in report.checks}


def test_current_markers_with_missing_canonical_unique_index_fail_schema_shape(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    SQLiteStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP INDEX idx_agent_definitions_one_active")

    report = _run(database)

    checks = _check_map(report)
    assert checks["database.integrity"] is HealthStatus.PASS
    assert checks["database.foreign-keys"] is HealthStatus.PASS
    assert checks["database.schema.shape"] is HealthStatus.FAIL
    assert report.overall is HealthStatus.FAIL


def test_current_markers_with_missing_canonical_foreign_key_fail_schema_shape(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    SQLiteStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE task_events")
        conn.execute(
            """CREATE TABLE task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                previous_state TEXT,
                new_state TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )

    report = _run(database)

    checks = _check_map(report)
    assert checks["database.integrity"] is HealthStatus.PASS
    assert checks["database.foreign-keys"] is HealthStatus.PASS
    assert checks["database.schema.shape"] is HealthStatus.FAIL
    assert report.overall is HealthStatus.FAIL
