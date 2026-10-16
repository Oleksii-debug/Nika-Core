from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.reliability.backup import SQLiteRecoveryManager


class _RecordingAudit:
    def __init__(self, store: SQLiteStore) -> None:
        self._delegate = AuditLog(store)
        self.event_types: list[str] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.event_types.append(event_type)
        return self._delegate.append(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )


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
                "restore-audit-continuity",
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
                "restore-audit-continuity",
            ),
        )
        conn.commit()


def test_restore_safety_backup_does_not_emit_discarded_live_audit_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = SQLiteStore(database)
    store.initialize()
    _insert_task(database, value="snapshot")

    recording_audit = _RecordingAudit(store)
    manager = SQLiteRecoveryManager(store, recording_audit)  # type: ignore[arg-type]
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    _set_task_value(database, "newer-live-value")
    plan = manager.prepare_restore(backup)

    recording_audit.event_types.clear()
    result = manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
    )

    assert result.safety_backup is not None
    assert recording_audit.event_types == []

    events = AuditLog(store).list_for(
        entity_type="database",
        entity_id=str(database.resolve()),
    )
    assert events[-1].event_type == "reliability.restore_completed"
    assert events[-1].payload["safety_backup_file"] == result.safety_backup.database_path.name
