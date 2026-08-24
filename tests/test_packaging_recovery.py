from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.recovery import (
    ReleaseDatabaseRecovery,
    ReleaseDatabaseRecoveryError,
)
from nika_core.reliability.backup import (
    BackupVerificationError,
    RestorePlanStaleError,
    SQLiteRecoveryManager,
)

SHA_OLD = "1" * 40
SHA_NEW = "2" * 40


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _insert_task(path: Path, *, task_id: str, value: str) -> None:
    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(path) as connection:
        now = "2026-08-23T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "release-recovery",
                "agent",
                "READY",
                payload,
                now,
                now,
            ),
        )


def _set_task_value(path: Path, *, task_id: str, value: str) -> None:
    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (payload, task_id),
        )


def _task_value(path: Path, *, task_id: str) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"missing task: {task_id}")
    return str(json.loads(row[0])["value"])


def _release_recovery(database: Path) -> ReleaseDatabaseRecovery:
    return ReleaseDatabaseRecovery(SQLiteRecoveryManager(_initialize(database)))


def _restarted_release_recovery(database: Path) -> ReleaseDatabaseRecovery:
    return ReleaseDatabaseRecovery(SQLiteRecoveryManager(SQLiteStore(database)))


def test_snapshot_reuses_canonical_backup_and_binds_exact_release(tmp_path: Path) -> None:
    database = tmp_path / "дані з пробілом" / "ніка.db"
    recovery = _release_recovery(database)
    _insert_task(database, task_id="snapshot", value="before")
    snapshot_dir = tmp_path / "release snapshots" / "old"

    snapshot = recovery.create_snapshot(
        snapshot_dir,
        source_release_sha=SHA_OLD,
    )
    verified = recovery.verify_snapshot(
        snapshot_dir,
        expected_source_release_sha=SHA_OLD,
    )

    assert verified == snapshot
    assert snapshot.source_release_sha == SHA_OLD
    assert {path.name for path in snapshot_dir.iterdir()} == {
        "database.sqlite3",
        "database.sqlite3.manifest.json",
        "release-database-snapshot.json",
    }
    assert _task_value(
        snapshot_dir / "database.sqlite3",
        task_id="snapshot",
    ) == "before"


def test_snapshot_path_is_idempotent_and_does_not_rebind_release(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    snapshot_dir = tmp_path / "snapshot"

    first = recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    replay = recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    assert replay == first

    with pytest.raises(ReleaseDatabaseRecoveryError, match="source SHA"):
        recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_NEW)


def test_snapshot_tamper_is_rejected_by_canonical_verifier(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    backup = snapshot_dir / "database.sqlite3"
    content = bytearray(backup.read_bytes())
    content[len(content) // 2] ^= 0x01
    backup.write_bytes(content)

    with pytest.raises(BackupVerificationError):
        recovery.verify_snapshot(snapshot_dir)


def test_release_metadata_strict_numeric_and_unknown_fields_fail(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    metadata = snapshot_dir / "release-database-snapshot.json"

    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["database_size"] = True
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseDatabaseRecoveryError, match="database_size"):
        recovery.verify_snapshot(snapshot_dir)

    payload["database_size"] = 1
    payload["unexpected"] = "field"
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseDatabaseRecoveryError, match="fields do not match"):
        recovery.verify_snapshot(snapshot_dir)


def test_release_metadata_duplicate_fields_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    metadata = snapshot_dir / "release-database-snapshot.json"

    original = metadata.read_text(encoding="utf-8").rstrip()
    payload = json.loads(original)
    duplicate = (
        original[:-1]
        + ',"source_release_sha":'
        + json.dumps(payload["source_release_sha"])
        + "}\n"
    )
    metadata.write_text(duplicate, encoding="utf-8")

    with pytest.raises(ReleaseDatabaseRecoveryError, match="duplicate fields"):
        recovery.verify_snapshot(snapshot_dir)


def test_restore_preview_requires_current_release_identity(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)

    with pytest.raises(ReleaseDatabaseRecoveryError, match="current release SHA"):
        recovery.prepare_restore(
            snapshot_dir,
            expected_source_release_sha=SHA_OLD,
            current_release_sha=None,
        )


def test_release_confirmation_binds_source_and_current_release(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    _insert_task(database, task_id="task", value="snapshot")
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    _set_task_value(database, task_id="task", value="current")

    plan = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=SHA_NEW,
    )
    with pytest.raises(PermissionError, match="confirmation"):
        recovery.restore(
            plan,
            confirmation_fingerprint="0" * 64,
        )

    result = recovery.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
    )
    assert result.snapshot.source_release_sha == SHA_OLD
    assert result.canonical_result.safety_backup is not None
    assert _task_value(database, task_id="task") == "snapshot"
    assert _task_value(
        result.canonical_result.safety_backup.database_path,
        task_id="task",
    ) == "current"


def test_canonical_stale_preview_guard_remains_authoritative(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    _insert_task(database, task_id="task", value="snapshot")
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    _set_task_value(database, task_id="task", value="preview")

    plan = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=SHA_NEW,
    )
    _set_task_value(database, task_id="task", value="changed-after-preview")

    with pytest.raises(RestorePlanStaleError):
        recovery.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )
    assert _task_value(database, task_id="task") == "changed-after-preview"


def test_snapshot_rejects_extra_files_and_noncanonical_release_sha(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    recovery = _release_recovery(database)
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    (snapshot_dir / "extra.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ReleaseDatabaseRecoveryError, match="contents"):
        recovery.verify_snapshot(snapshot_dir)

    with pytest.raises(ValueError, match="exact lowercase"):
        recovery.create_snapshot(
            tmp_path / "bad-sha",
            source_release_sha="A" * 40,
        )


def test_missing_live_database_requires_absent_current_release_sha(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source_recovery = _release_recovery(source)
    snapshot_dir = tmp_path / "snapshot"
    source_recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)

    target = tmp_path / "missing.db"
    recovery = ReleaseDatabaseRecovery(
        SQLiteRecoveryManager(SQLiteStore(target))
    )
    plan = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=None,
    )
    assert plan.canonical_plan.current_exists is False

    with pytest.raises(ReleaseDatabaseRecoveryError, match="must be absent"):
        recovery.prepare_restore(
            snapshot_dir,
            expected_source_release_sha=SHA_OLD,
            current_release_sha=SHA_NEW,
        )


def test_snapshot_restore_and_replay_survive_process_reconstruction(tmp_path: Path) -> None:
    database = tmp_path / "дані з пробілом" / "ніка.db"
    recovery = _release_recovery(database)
    _insert_task(database, task_id="restart", value="snapshot")
    snapshot_dir = tmp_path / "release snapshots" / "версія 1"

    first = recovery.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    _set_task_value(database, task_id="restart", value="current")

    restarted = _restarted_release_recovery(database)
    replay = restarted.create_snapshot(snapshot_dir, source_release_sha=SHA_OLD)
    assert replay == first
    plan = restarted.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=SHA_NEW,
    )
    result = restarted.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
    )
    assert result.snapshot == first
    assert _task_value(database, task_id="restart") == "snapshot"

    restarted_again = _restarted_release_recovery(database)
    assert restarted_again.verify_snapshot(
        snapshot_dir,
        expected_source_release_sha=SHA_OLD,
    ) == first
    assert restarted_again.recover_interrupted_restore() is None


def test_packaging_adapter_does_not_implement_a_second_sqlite_engine() -> None:
    import nika_core.packaging.recovery as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "import sqlite3" not in source
    assert "SQLiteRecoveryManager" in source
    assert ".create_backup(" in source
    assert ".verify_backup(" in source
    assert ".prepare_restore(" in source
    assert ".restore(" in source
    assert ".recover_interrupted_restore(" in source
