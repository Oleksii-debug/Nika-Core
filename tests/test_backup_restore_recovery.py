from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.reliability import backup as backup_module
from nika_core.reliability.backup import (
    BackupVerificationError,
    InterruptedRestoreDisposition,
    RestorePlanStaleError,
    RestoreSafetyError,
    SQLiteRecoveryManager,
)


class _SimulatedProcessLoss(BaseException):
    """Bypass ordinary Exception rollback like abrupt process termination."""


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _insert_task(
    path: Path,
    *,
    task_id: str,
    value: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "backup-proof",
                "agent",
                "READY",
                payload,
                now,
                now,
            ),
        )
        conn.commit()


def _set_task_value(path: Path, task_id: str, value: str) -> None:
    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
            (payload, datetime.now(UTC).isoformat(), task_id),
        )
        conn.commit()


def _task_value(path: Path, task_id: str) -> str:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"missing task: {task_id}")
    return str(json.loads(row[0])["value"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_version_one_database(path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for statement in MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            (now,),
        )
        conn.commit()


def test_live_wal_backup_is_consistent_and_verifiable(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    backup = tmp_path / "live-backup.db"

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        now = datetime.now(UTC).isoformat()
        writer.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wal-task",
                "backup-proof",
                "agent",
                "READY",
                json.dumps({"value": "committed-in-wal"}),
                now,
                now,
            ),
        )
        writer.commit()

        artifact = manager.create_backup(backup)
        verified = manager.verify_backup(backup)
        assert verified.sha256 == artifact.sha256
        assert verified.schema_version == SCHEMA_VERSION
        assert _task_value(backup, "wal-task") == "committed-in-wal"
    finally:
        writer.close()


def test_backup_tamper_is_rejected_before_restore(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    backup = tmp_path / "backup.db"
    manager = SQLiteRecoveryManager(store)
    manager.create_backup(backup)

    content = bytearray(backup.read_bytes())
    content[len(content) // 2] ^= 0x01
    backup.write_bytes(content)

    with pytest.raises(BackupVerificationError, match="SHA-256"):
        manager.verify_backup(backup)


def test_backup_rejects_foreign_key_corruption(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store)

    with closing(sqlite3.connect(database)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            INSERT INTO task_events(
                task_id, previous_state, new_state, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "missing-task",
                None,
                "READY",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()

    with pytest.raises(BackupVerificationError, match="foreign-key"):
        manager.create_backup(tmp_path / "bad-foreign-key.db")


def test_backup_rejects_migration_history_gap(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store)

    with closing(sqlite3.connect(database)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 4")
        conn.commit()

    with pytest.raises(BackupVerificationError, match="not contiguous"):
        manager.create_backup(tmp_path / "gap.db")


def test_restore_requires_exact_preview_and_rejects_stale_live_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    _insert_task(database, task_id="task", value="before")
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)

    plan = manager.prepare_restore(backup)
    with pytest.raises(PermissionError, match="confirmation"):
        manager.restore(
            plan,
            confirmation_fingerprint="0" * 64,
        )

    _set_task_value(database, "task", "changed-after-preview")
    with pytest.raises(RestorePlanStaleError, match="changed after restore preview"):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )
    assert _task_value(database, "task") == "changed-after-preview"


def test_healthy_restore_keeps_safety_backup_and_restores_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    _insert_task(database, task_id="task", value="snapshot")
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "task", "newer-live-value")

    plan = manager.prepare_restore(backup)
    result = manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
    )

    assert _task_value(database, "task") == "snapshot"
    assert result.restored_schema_version == SCHEMA_VERSION
    assert result.safety_backup is not None
    assert result.safety_backup.database_path.is_file()
    manager.verify_backup(result.safety_backup.database_path)
    assert _task_value(
        result.safety_backup.database_path,
        "task",
    ) == "newer-live-value"

    events = AuditLog(store).list_for(
        entity_type="database",
        entity_id=str(database.resolve()),
    )
    assert events[-1].event_type == "reliability.restore_completed"


def test_restore_migrates_old_valid_backup_before_touching_live(
    tmp_path: Path,
) -> None:
    old_database = tmp_path / "old-v1.db"
    _create_version_one_database(old_database)
    _insert_task(old_database, task_id="old-task", value="from-v1")
    old_backup = tmp_path / "old-v1-backup.db"
    SQLiteRecoveryManager(SQLiteStore(old_database)).create_backup(old_backup)

    live_database = tmp_path / "nika.db"
    live_store = _initialize(live_database)
    _insert_task(live_database, task_id="live-task", value="current")
    manager = SQLiteRecoveryManager(live_store, AuditLog(live_store))

    plan = manager.prepare_restore(old_backup)
    result = manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
    )

    assert result.restored_schema_version == SCHEMA_VERSION
    assert SQLiteStore(live_database).schema_version() == SCHEMA_VERSION
    assert _task_value(live_database, "old-task") == "from-v1"


def test_corrupt_current_requires_explicit_override_and_quarantines_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    _insert_task(database, task_id="task", value="recover-me")
    backup = tmp_path / "known-good.db"
    manager.create_backup(backup)

    corrupt_bytes = b"not-a-sqlite-database\x00" * 97
    database.write_bytes(corrupt_bytes)
    corrupt_sha = _sha256(database)

    plan = manager.prepare_restore(backup)
    assert plan.current_exists is True
    assert plan.current_is_healthy is False
    with pytest.raises(RestoreSafetyError, match="explicit review"):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )
    assert _sha256(database) == corrupt_sha

    plan = manager.prepare_restore(backup)
    result = manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
        allow_replace_unrecoverable_current=True,
    )

    assert _task_value(database, "task") == "recover-me"
    assert result.quarantined_database is not None
    assert _sha256(result.quarantined_database) == corrupt_sha
    assert result.quarantine_manifest is not None
    manifest = json.loads(result.quarantine_manifest.read_text(encoding="utf-8"))
    assert manifest["trusted_database"] is False
    assert manifest["pre_restore_sha256"] == corrupt_sha
    assert not manager._restore_marker_path(database).exists()


def test_process_loss_during_corrupt_replacement_is_recovered_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    _insert_task(database, task_id="task", value="recover-after-crash")
    backup = tmp_path / "known-good.db"
    manager.create_backup(backup)
    database.write_bytes(b"destroyed-header" * 257)
    plan = manager.prepare_restore(backup)

    real_replace = os.replace

    def process_loss_replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        is_stage_install = (
            ".restore-stage." in source_path.name
            and destination_path.resolve() == database.resolve()
        )
        if is_stage_install:
            raise _SimulatedProcessLoss()
        real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", process_loss_replace)
    with pytest.raises(_SimulatedProcessLoss):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
            allow_replace_unrecoverable_current=True,
        )

    marker_path = manager._restore_marker_path(database)
    assert marker_path.exists()
    assert not database.exists()

    monkeypatch.setattr(backup_module.os, "replace", real_replace)
    restarted = SQLiteRecoveryManager(SQLiteStore(database))
    recovered = restarted.recover_interrupted_restore()

    assert recovered is not None
    assert recovered.disposition == InterruptedRestoreDisposition.COMPLETED
    assert _task_value(database, "task") == "recover-after-crash"
    assert not marker_path.exists()
    assert recovered.quarantined_database is not None
    assert recovered.quarantined_database.exists()


def test_post_copy_validation_failure_rolls_back_safety_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    _insert_task(database, task_id="task", value="snapshot")
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "task", "must-survive-failed-restore")
    plan = manager.prepare_restore(backup)

    real_validate = SQLiteRecoveryManager._validate_database
    fired = False

    def fail_once(path: Path, *, require_supported: bool) -> int:
        nonlocal fired
        if Path(path).resolve() == database.resolve() and not fired:
            fired = True
            raise BackupVerificationError("injected post-copy validation failure")
        return real_validate(path, require_supported=require_supported)

    monkeypatch.setattr(
        SQLiteRecoveryManager,
        "_validate_database",
        staticmethod(fail_once),
    )
    with pytest.raises(BackupVerificationError, match="injected post-copy"):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )

    assert fired is True
    assert _task_value(database, "task") == "must-survive-failed-restore"
