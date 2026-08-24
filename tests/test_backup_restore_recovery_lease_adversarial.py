from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability import backup as backup_module
from nika_core.reliability.backup import (
    BackupVerificationError,
    RestoreSafetyError,
    SQLiteRecoveryManager,
)


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
                "recovery-adversarial",
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
                "recovery-adversarial",
            ),
        )
        connection.commit()


def _task_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            ("recovery-adversarial",),
        ).fetchone()
    if row is None:
        raise AssertionError("missing recovery-adversarial task")
    return str(json.loads(row[0])["value"])


def test_backup_wal_mutation_after_preview_is_rejected_before_restore(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="snapshot")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)

    _set_task_value(database, "must-survive")
    plan = manager.prepare_restore(backup)

    writer = sqlite3.connect(backup)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute(
            "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
            (
                json.dumps({"value": "mutated-backup"}, sort_keys=True),
                datetime.now(UTC).isoformat(),
                "recovery-adversarial",
            ),
        )
        writer.commit()

        wal = backup.with_name(f"{backup.name}-wal")
        assert wal.is_file()
        assert wal.stat().st_size > 0

        with pytest.raises(
            BackupVerificationError,
            match="sidecar state not bound by its manifest",
        ):
            manager.restore(
                plan,
                confirmation_fingerprint=plan.confirmation_fingerprint,
            )

        assert _task_value(database) == "must-survive"
    finally:
        writer.close()


def test_quarantine_rollback_refuses_unknown_competing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="known-good")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "known-good.db"
    manager.create_backup(backup)

    corrupt_bytes = b"corrupt-pre-restore" * 137
    database.write_bytes(corrupt_bytes)
    plan = manager.prepare_restore(backup)
    marker = manager._restore_marker_path(database)
    competing_bytes = b"independent-competing-target" * 89
    real_link = os.link

    def competing_link(source_path, target_path, *args, **kwargs) -> None:
        destination = Path(target_path)
        if destination.resolve() == database.resolve() and not database.exists():
            database.write_bytes(competing_bytes)
        real_link(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(backup_module.os, "link", competing_link)

    with pytest.raises(
        RestoreSafetyError,
        match="replacement and quarantine rollback both failed",
    ):
        manager.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
            allow_replace_unrecoverable_current=True,
        )

    assert database.read_bytes() == competing_bytes
    assert marker.is_file()
    quarantine = tuple(tmp_path.glob("nika.db.unrecoverable-*.sqlite3"))
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == corrupt_bytes
