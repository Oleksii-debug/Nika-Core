from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.diagnostics import HealthService, HealthStatus
from nika_core.resources.contracts import ResourceSnapshot

_FIXED_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
_OBSERVER = ResourceSnapshot(10.0, 20.0, 1024)


class _StaticObserver:
    def snapshot(self) -> ResourceSnapshot:
        return _OBSERVER


def _write_healthy_database(path: Path) -> None:
    SQLiteStore(path).initialize()


def _run(path: Path):
    return HealthService(
        AppConfig(database_path=path, model_provider="mock", schema_version=1),
        resource_observer=_StaticObserver(),
        clock=lambda: _FIXED_NOW,
    ).run()


def _check_map(report) -> dict[str, HealthStatus]:
    return {check.check_id: check.status for check in report.checks}


def _database_family_state(path: Path) -> dict[str, tuple[int, int, bytes]]:
    return {
        candidate.name: (
            candidate.stat().st_size,
            candidate.stat().st_mtime_ns,
            candidate.read_bytes(),
        )
        for candidate in sorted(path.parent.iterdir())
        if candidate.name.startswith(path.name)
    }


def test_active_delete_journal_snapshot_recovers_committed_state_without_source_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rollback health" / "nika.db"
    _write_healthy_database(database)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        writer.execute("PRAGMA cache_size=1")
        writer.execute("PRAGMA cache_spill=ON")
        writer.execute("CREATE TABLE health_rollback_probe(value TEXT NOT NULL)")
        writer.execute("INSERT INTO health_rollback_probe(value) VALUES ('committed')")
        writer.commit()
        committed_size = database.stat().st_size

        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DROP TABLE tasks")
        writer.executemany(
            "INSERT INTO health_rollback_probe(value) VALUES (?)",
            (("x" * 8192,) for _ in range(128)),
        )
        journal = Path(f"{database}-journal")
        assert journal.exists()
        assert database.stat().st_size > committed_size
        before = _database_family_state(database)

        report = _run(database)

        after = _database_family_state(database)
        checks = _check_map(report)
        assert report.overall is HealthStatus.PASS
        assert checks["database.integrity"] is HealthStatus.PASS
        assert checks["database.schema.shape"] is HealthStatus.PASS
        assert after == before
    finally:
        writer.rollback()
        writer.close()

    with sqlite3.connect(database) as reader:
        assert reader.execute("SELECT COUNT(*) FROM health_rollback_probe").fetchone() == (1,)
        assert reader.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone() == (1,)


def test_snapshot_fails_closed_when_rollback_journal_appears_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "journal-create-race.db"
    _write_healthy_database(database)
    writer = sqlite3.connect(database)
    writer.execute("CREATE TABLE health_race_probe(value TEXT NOT NULL)")
    writer.commit()
    journal = Path(f"{database}-journal")
    assert not journal.exists()

    original_copy = HealthService._copy_stable_file.__func__
    raced = False
    active_state: dict[str, tuple[int, int, bytes]] | None = None

    def racing_copy(cls, source: Path, destination: Path):
        nonlocal active_state, raced
        copied = original_copy(cls, source, destination)
        if source == database and not raced:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO health_race_probe(value) VALUES ('uncommitted')")
            assert journal.exists()
            active_state = _database_family_state(database)
            raced = True
        return copied

    monkeypatch.setattr(HealthService, "_copy_stable_file", classmethod(racing_copy))
    try:
        report = _run(database)

        assert raced is True
        assert active_state is not None
        assert _check_map(report)["database.open"] is HealthStatus.FAIL
        assert _database_family_state(database) == active_state
    finally:
        writer.rollback()
        writer.close()

    assert _run(database).overall is HealthStatus.PASS


def test_snapshot_fails_closed_when_rollback_journal_disappears_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "journal-remove-race.db"
    _write_healthy_database(database)
    writer = sqlite3.connect(database)
    writer.execute("CREATE TABLE health_race_probe(value TEXT NOT NULL)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO health_race_probe(value) VALUES ('uncommitted')")
    journal = Path(f"{database}-journal")
    assert journal.exists()

    original_copy = HealthService._copy_stable_file.__func__
    raced = False

    def racing_copy(cls, source: Path, destination: Path):
        nonlocal raced
        copied = original_copy(cls, source, destination)
        if source == database and not raced:
            writer.rollback()
            assert not journal.exists()
            raced = True
        return copied

    monkeypatch.setattr(HealthService, "_copy_stable_file", classmethod(racing_copy))
    try:
        report = _run(database)

        assert raced is True
        assert _check_map(report)["database.open"] is HealthStatus.FAIL
        assert not journal.exists()
        assert writer.execute("SELECT COUNT(*) FROM health_race_probe").fetchone() == (0,)
    finally:
        writer.rollback()
        writer.close()

    assert _run(database).overall is HealthStatus.PASS
