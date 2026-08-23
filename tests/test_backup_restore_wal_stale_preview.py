from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability.backup import RestorePlanStaleError, SQLiteRecoveryManager


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


def _insert_task(path: Path, *, value: str) -> None:
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
                "wal-stale-preview",
                "backup-proof",
                "agent",
                "READY",
                payload,
                now,
                now,
            ),
        )
        conn.commit()


def _task_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            ("wal-stale-preview",),
        ).fetchone()
    if row is None:
        raise AssertionError("missing WAL stale-preview task")
    return str(json.loads(row[0])["value"])


def test_restore_rejects_committed_wal_only_change_after_preview(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    store = _initialize(database)
    _insert_task(database, value="approved-preview-state")
    manager = SQLiteRecoveryManager(store)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)

    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        plan = manager.prepare_restore(backup)
        raw_main_sha_after_preview = _sha256(database)

        payload = json.dumps(
            {"value": "committed-after-preview"},
            ensure_ascii=False,
            sort_keys=True,
        )
        writer.execute(
            "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
            (
                payload,
                datetime.now(UTC).isoformat(),
                "wal-stale-preview",
            ),
        )
        writer.commit()

        wal_path = database.with_name(f"{database.name}-wal")
        assert wal_path.is_file()
        assert wal_path.stat().st_size > 0
        assert _sha256(database) == raw_main_sha_after_preview
        assert _task_value(database) == "committed-after-preview"

        with pytest.raises(RestorePlanStaleError, match="changed after restore preview"):
            manager.restore(
                plan,
                confirmation_fingerprint=plan.confirmation_fingerprint,
            )

        assert _task_value(database) == "committed-after-preview"
    finally:
        writer.close()
