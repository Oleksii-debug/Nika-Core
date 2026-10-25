from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.reliability import backup as backup_module
from nika_core.reliability.backup import (
    InterruptedRestoreDisposition,
    RestorePlanStaleError,
    RestoreSafetyError,
    SQLiteRecoveryManager,
)
from nika_core.reliability.recovery_lease import RecoveryFileLease


class _SimulatedProcessLoss(BaseException):
    """Bypass ordinary Exception rollback like abrupt process termination."""


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _insert_task(path: Path, *, value: str) -> None:
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "recovery-lease",
                "backup-proof",
                "agent",
                "READY",
                json.dumps({"value": value}, sort_keys=True),
                now,
                now,
            ),
        )
        conn.commit()


def _set_task_value(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
            (
                json.dumps({"value": value}, sort_keys=True),
                datetime.now(UTC).isoformat(),
                "recovery-lease",
            ),
        )
        conn.commit()


def _task_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            ("recovery-lease",),
        ).fetchone()
    if row is None:
        raise AssertionError("missing recovery-lease task")
    return str(json.loads(row[0])["value"])


def _hold_file_lease(
    lease_path: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with RecoveryFileLease(Path(lease_path)):
        ready.set()
        if not release.wait(20):
            raise AssertionError("parent did not release recovery lease worker")


def test_zero_length_wal_is_same_logical_durable_state(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    manager = SQLiteRecoveryManager(_initialize(database))

    without_wal = manager._restore_state_sha256(database)
    wal = database.with_name(f"{database.name}-wal")
    wal.touch()
    assert wal.stat().st_size == 0

    assert manager._restore_state_sha256(database) == without_wal


def test_live_wal_writer_blocks_restore_until_writer_ownership_is_free(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)

    keeper = sqlite3.connect(database)
    try:
        assert keeper.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        _set_task_value(database, "newer-live-state")
        plan = manager.prepare_restore(backup)
        keeper.execute("BEGIN IMMEDIATE")

        with pytest.raises(RestoreSafetyError, match="writer prevents exclusive recovery ownership"):
            manager.restore(
                plan,
                confirmation_fingerprint=plan.confirmation_fingerprint,
            )

        assert _task_value(database) == "newer-live-state"
        assert not tuple(tmp_path.glob("nika.db.pre-restore-*.sqlite3"))
    finally:
        if keeper.in_transaction:
            keeper.execute("ROLLBACK")
        keeper.close()

    refreshed = manager.prepare_restore(backup)
    manager.restore(
        refreshed,
        confirmation_fingerprint=refreshed.confirmation_fingerprint,
    )
    assert _task_value(database) == "snapshot"


def test_cross_process_recovery_lease_rejects_second_restore_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "newer")
    plan = manager.prepare_restore(backup)

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    lease_path = manager._recovery_lease_path(database.resolve())
    process = context.Process(
        target=_hold_file_lease,
        args=(str(lease_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(20), "child did not acquire recovery lease"
        with pytest.raises(RestoreSafetyError, match="owns the recovery lease"):
            manager.restore(
                plan,
                confirmation_fingerprint=plan.confirmation_fingerprint,
            )
        assert _task_value(database) == "newer"
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert process.exitcode == 0


def test_process_loss_after_safety_backup_before_live_copy_preserves_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "must-survive")
    plan = manager.prepare_restore(backup)

    def lose_process(*args, **kwargs) -> None:
        raise _SimulatedProcessLoss()

    monkeypatch.setattr(manager, "_append_restore_completed", lose_process)
    with pytest.raises(_SimulatedProcessLoss):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )

    assert _task_value(database) == "must-survive"
    safety_paths = tuple(tmp_path.glob("nika.db.pre-restore-*.sqlite3"))
    assert len(safety_paths) == 1
    safety = manager.verify_backup(safety_paths[0])
    assert safety.schema_version == SCHEMA_VERSION
    assert _task_value(safety.database_path) == "must-survive"


def test_process_loss_after_healthy_live_copy_keeps_complete_restore_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "newer")
    plan = manager.prepare_restore(backup)

    real_copy = SQLiteRecoveryManager._copy_database_to_connection

    def copy_then_lose_process(
        source: Path,
        destination_connection: sqlite3.Connection,
    ) -> None:
        real_copy(source, destination_connection)
        raise _SimulatedProcessLoss()

    monkeypatch.setattr(
        SQLiteRecoveryManager,
        "_copy_database_to_connection",
        staticmethod(copy_then_lose_process),
    )
    with pytest.raises(_SimulatedProcessLoss):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )

    assert _task_value(database) == "snapshot"
    events = AuditLog(store).list_for(
        entity_type="database",
        entity_id=str(database.resolve()),
    )
    assert events[-1].event_type == "reliability.restore_completed"
    safety_paths = tuple(tmp_path.glob("nika.db.pre-restore-*.sqlite3"))
    assert len(safety_paths) == 1
    manager.verify_backup(safety_paths[0])


def test_process_loss_after_corrupt_stage_publication_recovers_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="restored")
    manager = SQLiteRecoveryManager(store, AuditLog(store))
    backup = tmp_path / "known-good.db"
    manager.create_backup(backup)
    database.write_bytes(b"corrupt-current-bytes" * 137)
    plan = manager.prepare_restore(backup)

    real_publish = SQLiteRecoveryManager._publish_quarantine_manifest

    def lose_after_publication(self, marker, target):
        raise _SimulatedProcessLoss()

    monkeypatch.setattr(
        SQLiteRecoveryManager,
        "_publish_quarantine_manifest",
        lose_after_publication,
    )
    with pytest.raises(_SimulatedProcessLoss):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
            allow_replace_unrecoverable_current=True,
        )

    marker_path = manager._restore_marker_path(database)
    assert marker_path.exists()
    assert _task_value(database) == "restored"

    monkeypatch.setattr(
        SQLiteRecoveryManager,
        "_publish_quarantine_manifest",
        real_publish,
    )
    restarted = SQLiteRecoveryManager(SQLiteStore(database))
    recovered = restarted.recover_interrupted_restore()
    assert recovered is not None
    assert recovered.disposition == InterruptedRestoreDisposition.COMPLETED
    assert _task_value(database) == "restored"
    assert not marker_path.exists()


def test_missing_target_publication_never_clobbers_competing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    source_store = _initialize(source)
    _insert_task(source, value="backup-value")
    backup = tmp_path / "known-good.db"
    SQLiteRecoveryManager(source_store).create_backup(backup)

    target = tmp_path / "target.db"
    manager = SQLiteRecoveryManager(SQLiteStore(target))
    plan = manager.prepare_restore(backup)
    real_link = os.link

    def competing_link(source_path, target_path, *args, **kwargs) -> None:
        destination = Path(target_path)
        if destination.resolve() == target.resolve() and not target.exists():
            _initialize(target)
        real_link(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(backup_module.os, "link", competing_link)
    with pytest.raises(RestorePlanStaleError, match="appeared before publication"):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )

    assert target.exists()
    assert SQLiteStore(target).schema_version() == SCHEMA_VERSION
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute(
            "SELECT count(*) FROM tasks WHERE task_id = ?",
            ("recovery-lease",),
        ).fetchone()[0] == 0


def test_sidecar_directory_is_rejected_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source_store = _initialize(source)
    backup = tmp_path / "known-good.db"
    SQLiteRecoveryManager(source_store).create_backup(backup)

    target = tmp_path / "target.db"
    _initialize(target)
    wal = target.with_name(f"{target.name}-wal")
    wal.mkdir()

    with pytest.raises(RestoreSafetyError, match="sidecar is not a regular file"):
        SQLiteRecoveryManager(SQLiteStore(target)).prepare_restore(backup)
