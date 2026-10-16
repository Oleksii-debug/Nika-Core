from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.reliability.backup import (
    InterruptedRestoreDisposition,
    RestoreSafetyError,
    SQLiteRecoveryManager,
)

_CRASH_BEFORE_LIVE_COPY = 71
_CRASH_AFTER_LIVE_COPY = 72
_CRASH_BEFORE_QUARANTINE_PUBLICATION = 73


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _insert_task(path: Path, *, value: str) -> None:
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "physical-process-loss",
                "backup-proof",
                "agent",
                "READY",
                json.dumps({"value": value}, sort_keys=True),
                now,
                now,
            ),
        )
        connection.commit()


def _set_task_value(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
            (
                json.dumps({"value": value}, sort_keys=True),
                datetime.now(UTC).isoformat(),
                "physical-process-loss",
            ),
        )
        connection.commit()


def _task_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            ("physical-process-loss",),
        ).fetchone()
    if row is None:
        raise AssertionError("missing physical-process-loss task")
    return str(json.loads(row[0])["value"])


def _hold_sqlite_writer_reservation(
    database_path: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    connection = sqlite3.connect(database_path, timeout=5.0)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.execute("BEGIN IMMEDIATE")
        ready.set()
        if not release.wait(20):
            raise AssertionError("parent did not release SQLite writer reservation")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def _crash_before_live_copy(database_path: str, backup_path: str) -> None:
    store = SQLiteStore(database_path)
    manager = SQLiteRecoveryManager(store)
    plan = manager.prepare_restore(backup_path)

    def crash_before_stage_audit(*args: object, **kwargs: object) -> None:
        os._exit(_CRASH_BEFORE_LIVE_COPY)

    manager._append_restore_completed = crash_before_stage_audit  # type: ignore[method-assign]
    manager.restore(plan, confirmation_fingerprint=plan.confirmation_fingerprint)
    raise AssertionError("restore unexpectedly returned before physical process loss")


def _crash_after_live_copy(database_path: str, backup_path: str) -> None:
    store = SQLiteStore(database_path)
    manager = SQLiteRecoveryManager(store)
    plan = manager.prepare_restore(backup_path)
    real_copy = SQLiteRecoveryManager._copy_database_to_connection

    def copy_then_exit(
        source: Path,
        destination_connection: sqlite3.Connection,
    ) -> None:
        real_copy(source, destination_connection)
        os._exit(_CRASH_AFTER_LIVE_COPY)

    SQLiteRecoveryManager._copy_database_to_connection = staticmethod(copy_then_exit)
    manager.restore(plan, confirmation_fingerprint=plan.confirmation_fingerprint)
    raise AssertionError("restore unexpectedly returned before physical process loss")


def _crash_before_quarantine_publication(database_path: str, backup_path: str) -> None:
    store = SQLiteStore(database_path)
    manager = SQLiteRecoveryManager(store)
    plan = manager.prepare_restore(backup_path)

    def exit_before_publish(staged: Path, target: Path) -> None:
        os._exit(_CRASH_BEFORE_QUARANTINE_PUBLICATION)

    manager._publish_database_no_clobber = exit_before_publish  # type: ignore[method-assign]
    manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
        allow_replace_unrecoverable_current=True,
    )
    raise AssertionError("restore unexpectedly returned before physical process loss")


def _spawn_and_require_exit(
    target: object,
    args: tuple[str, ...],
    expected_exit_code: int,
) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=target, args=args)
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(10)
        raise AssertionError("recovery process did not terminate at the crash boundary")
    assert process.exitcode == expected_exit_code


def test_cross_process_sqlite_writer_blocks_native_recovery_ownership(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cross process sqlite.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "must-survive")
    with closing(sqlite3.connect(database)) as setup:
        assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    plan = manager.prepare_restore(backup)

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_sqlite_writer_reservation,
        args=(str(database), ready, release),
    )
    process.start()
    try:
        assert ready.wait(20), "child SQLite writer did not reserve WAL write ownership"
        try:
            manager.restore(
                plan,
                confirmation_fingerprint=plan.confirmation_fingerprint,
            )
        except RestoreSafetyError as exc:
            assert "writer prevents exclusive recovery ownership" in str(exc)
        else:
            raise AssertionError("restore bypassed a live cross-process SQLite writer")
        assert _task_value(database) == "must-survive"
        assert not tuple(tmp_path.glob("cross process sqlite.db.pre-restore-*.sqlite3"))
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert process.exitcode == 0


def test_real_process_loss_after_safety_backup_before_live_copy_preserves_live(
    tmp_path: Path,
) -> None:
    database = tmp_path / "before live copy.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "before-live-snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "must-survive")

    _spawn_and_require_exit(
        _crash_before_live_copy,
        (str(database), str(backup)),
        _CRASH_BEFORE_LIVE_COPY,
    )

    assert _task_value(database) == "must-survive"
    safety_paths = tuple(tmp_path.glob("before live copy.db.pre-restore-*.sqlite3"))
    assert len(safety_paths) == 1
    safety = SQLiteRecoveryManager(SQLiteStore(database)).verify_backup(safety_paths[0])
    assert _task_value(safety.database_path) == "must-survive"


def test_real_process_loss_after_committed_live_copy_keeps_restore_and_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "after live copy.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "after-live-snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "newer-live")

    _spawn_and_require_exit(
        _crash_after_live_copy,
        (str(database), str(backup)),
        _CRASH_AFTER_LIVE_COPY,
    )

    assert _task_value(database) == "snapshot"
    events = AuditLog(SQLiteStore(database)).list_for(
        entity_type="database",
        entity_id=str(database.resolve()),
    )
    assert events[-1].event_type == "reliability.restore_completed"
    safety_paths = tuple(tmp_path.glob("after live copy.db.pre-restore-*.sqlite3"))
    assert len(safety_paths) == 1
    SQLiteRecoveryManager(SQLiteStore(database)).verify_backup(safety_paths[0])


def test_real_process_loss_after_quarantine_before_publication_recovers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "quarantine crash.db"
    store = _initialize(database)
    _insert_task(database, value="restored")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "quarantine-snapshot.db"
    manager.create_backup(backup)
    corrupt_bytes = b"physical-corrupt-current" * 113
    database.write_bytes(corrupt_bytes)

    _spawn_and_require_exit(
        _crash_before_quarantine_publication,
        (str(database), str(backup)),
        _CRASH_BEFORE_QUARANTINE_PUBLICATION,
    )

    assert not database.exists()
    marker = SQLiteRecoveryManager._restore_marker_path(database)
    assert marker.is_file()
    quarantine = tuple(tmp_path.glob("quarantine crash.db.unrecoverable-*.sqlite3"))
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == corrupt_bytes

    restarted = SQLiteRecoveryManager(SQLiteStore(database))
    recovered = restarted.recover_interrupted_restore()
    assert recovered is not None
    assert recovered.disposition == InterruptedRestoreDisposition.COMPLETED
    assert _task_value(database) == "restored"
    assert not marker.exists()
