from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability.backup import RestoreSafetyError, SQLiteRecoveryManager


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_missing_target_preview_does_not_create_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source_store = _initialize(source)
    backup = tmp_path / "known-good.db"
    SQLiteRecoveryManager(source_store).create_backup(backup)

    target = tmp_path / "missing-target.db"
    manager = SQLiteRecoveryManager(SQLiteStore(target))
    plan = manager.prepare_restore(backup)

    assert plan.current_exists is False
    assert plan.current_sha256 is None
    assert not target.exists()

    result = manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
    )
    assert result.safety_backup is None
    assert target.exists()
    assert SQLiteStore(target).schema_version() == SCHEMA_VERSION


def test_preview_does_not_modify_newer_schema_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source_store = _initialize(source)
    backup = tmp_path / "known-good.db"
    SQLiteRecoveryManager(source_store).create_backup(backup)

    target = tmp_path / "future.db"
    _initialize(target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    before_sha = _sha256(target)

    manager = SQLiteRecoveryManager(SQLiteStore(target))
    plan = manager.prepare_restore(backup)

    assert plan.current_schema_version == SCHEMA_VERSION + 1
    assert plan.current_is_healthy is False
    assert _sha256(target) == before_sha
    with pytest.raises(RestoreSafetyError, match="explicit review"):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )
    assert _sha256(target) == before_sha


def test_interrupted_restore_marker_rejects_path_escape(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store)
    before_sha = _sha256(database)
    marker_path = manager._restore_marker_path(database)
    outside = tmp_path.parent / "outside.db"
    outside.unlink(missing_ok=True)
    marker = {
        "format_version": 1,
        "target_file": database.name,
        "stage_file": "../outside.db",
        "stage_sha256": "1" * 64,
        "quarantine_file": "quarantine.db",
        "quarantine_wal_file": "quarantine.db-wal",
        "quarantine_shm_file": "quarantine.db-shm",
        "current_sha256": before_sha,
        "backup_sha256": "2" * 64,
        "created_at": datetime.now(UTC).isoformat(),
    }
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(RestoreSafetyError, match="unsafe path"):
        manager.recover_interrupted_restore()
    assert _sha256(database) == before_sha
    assert not outside.exists()
