from __future__ import annotations

from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


def test_v13_upgrade_backfills_reservation_generation_without_losing_evidence(tmp_path) -> None:
    database = tmp_path / "nika-v13.db"
    store = SQLiteStore(database)
    created_at = "2026-10-05T00:00:00+00:00"

    # Minimal faithful v13 carrier for the table owned by this migration. Mark the
    # canonical schema as already at v13 so initialize() exercises only v14.
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (13, ?)",
            (created_at,),
        )
        conn.execute(
            """CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE idempotency_records (
                operation_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            )"""
        )
        conn.execute(
            """INSERT INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "task-upgrade",
                "proof",
                "runtime",
                "running",
                "{}",
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """INSERT INTO idempotency_records(
                operation_key, task_id, operation_type, input_fingerprint,
                status, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                "effect:upgrade",
                "task-upgrade",
                "tool.external_effect",
                "fingerprint-v1",
                IdempotencyStatus.PENDING.value,
                created_at,
                created_at,
            ),
        )

    store.initialize()

    assert SCHEMA_VERSION == 14
    assert store.schema_version() == SCHEMA_VERSION
    record = IdempotencyLedger(store).require("effect:upgrade")
    assert record.task_id == "task-upgrade"
    assert record.input_fingerprint == "fingerprint-v1"
    assert record.status is IdempotencyStatus.PENDING
    assert record.created_at == created_at
    assert len(record.reservation_generation) == 32
    assert set(record.reservation_generation) <= set("0123456789abcdef")

    generation = record.reservation_generation
    store.initialize()
    assert IdempotencyLedger(store).require("effect:upgrade").reservation_generation == generation
