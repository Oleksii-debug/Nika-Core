from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.multi_agent_state_schema import MULTI_AGENT_STATE_SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.diagnostics import HealthService, HealthStatus
from nika_core.resources.contracts import ResourceSnapshot

_FIXED_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


class _StaticObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(10.0, 20.0, 1024)


def _run(path: Path):
    return HealthService(
        AppConfig(database_path=path, model_provider="mock", schema_version=1),
        resource_observer=_StaticObserver(),
        clock=lambda: _FIXED_NOW,
    ).run()


def test_future_multi_agent_state_migration_fails_health_like_canonical_startup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    SQLiteStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO multi_agent_state_schema_migrations(version, applied_at) "
            "VALUES (?, 'future')",
            (MULTI_AGENT_STATE_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteStore(database).initialize()

    report = _run(database)
    checks = {check.check_id: check.status for check in report.checks}

    assert checks["database.schema.core"] is HealthStatus.PASS
    assert checks["database.schema.multi-agent-state"] is HealthStatus.FAIL
    assert checks["database.schema.product-project"] is HealthStatus.PASS
    assert checks["database.schema.shape"] is HealthStatus.PASS
    assert report.overall is HealthStatus.FAIL
