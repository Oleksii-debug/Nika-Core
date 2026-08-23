from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditIntegrityError, AuditLog


_INTEGRITY_KEY = "_nika_audit_integrity"
_REDACTED = "[REDACTED]"


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_audit_chain_round_trip_and_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    first = audit.append(
        event_type="task.created",
        entity_type="task",
        entity_id="task-1",
        payload={"state": "queued"},
    )
    second = audit.append(
        event_type="task.started",
        entity_type="task",
        entity_id="task-1",
        payload={"state": "running"},
    )

    assert (first, second) == (1, 2)
    report = audit.verify_integrity()
    assert report.event_count == 2
    assert report.sealed_event_count == 2
    assert report.legacy_event_count == 0
    assert report.integrity_active is True
    assert report.head_event_id == 2
    assert report.head_sha256 is not None
    assert len(report.head_sha256) == 64

    events = audit.list_for(entity_type="task", entity_id="task-1")
    assert [event.payload for event in events] == [
        {"state": "queued"},
        {"state": "running"},
    ]


def test_audit_redacts_nested_secrets_before_persistence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    secrets = {
        "approval": "approval-value-should-not-persist",
        "password": "password-value-should-not-persist",
        "cookie": "cookie-value-should-not-persist",
        "authorization": "Bearer auth-value-should-not-persist",
    }

    audit.append(
        event_type="approval.checked",
        entity_type="approval",
        entity_id="approval-1",
        payload={
            "approval_token": secrets["approval"],
            "nested": {
                "password": secrets["password"],
                "cookie": secrets["cookie"],
            },
            "authorization_header": secrets["authorization"],
            "token_count": 3,
            "safe": "visible",
        },
    )

    event = audit.list_for(entity_type="approval", entity_id="approval-1")[0]
    assert event.payload == {
        "approval_token": _REDACTED,
        "nested": {
            "password": _REDACTED,
            "cookie": _REDACTED,
        },
        "authorization_header": _REDACTED,
        "token_count": 3,
        "safe": "visible",
    }

    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )
    for secret in secrets.values():
        assert secret not in raw
    assert _INTEGRITY_KEY in raw
    audit.verify_integrity()


def test_audit_rejects_reserved_integrity_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    with pytest.raises(ValueError, match="reserved"):
        audit.append(
            event_type="forged",
            entity_type="test",
            entity_id="1",
            payload={_INTEGRITY_KEY: {"event_sha256": "forged"}},
        )

    assert audit.verify_integrity().event_count == 0


def test_audit_payload_tamper_blocks_verification_read_and_next_append(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="created",
        entity_type="task",
        entity_id="task-1",
        payload={"state": "queued"},
    )
    audit.append(
        event_type="started",
        entity_type="task",
        entity_id="task-1",
        payload={"state": "running"},
    )

    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )
        raw["state"] = "forged"
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 1",
            (json.dumps(raw),),
        )

    with pytest.raises(AuditIntegrityError, match="digest does not match"):
        audit.verify_integrity()
    with pytest.raises(AuditIntegrityError):
        audit.list_for(entity_type="task", entity_id="task-1")
    with pytest.raises(AuditIntegrityError):
        audit.append(
            event_type="finished",
            entity_type="task",
            entity_id="task-1",
        )

    with store.connection() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        sequence = int(
            conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'audit_events'"
            ).fetchone()[0]
        )
    assert count == 2
    assert sequence == 2


@pytest.mark.parametrize(
    ("statement", "replacement"),
    [
        ("UPDATE audit_events SET event_type = ? WHERE event_id = 1", "forged.type"),
        ("UPDATE audit_events SET entity_id = ? WHERE event_id = 1", "forged-id"),
        (
            "UPDATE audit_events SET created_at = ? WHERE event_id = 1",
            "2000-01-01T00:00:00+00:00",
        ),
    ],
)
def test_audit_chain_binds_identity_and_timestamp(
    tmp_path: Path,
    statement: str,
    replacement: str,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="created",
        entity_type="task",
        entity_id="task-1",
        payload={"state": "queued"},
    )

    with store.connection() as conn:
        conn.execute(statement, (replacement,))

    with pytest.raises(AuditIntegrityError, match="digest does not match"):
        audit.verify_integrity()


@pytest.mark.parametrize("deleted_event_id", [2, 3])
def test_audit_detects_middle_and_tail_deletion(
    tmp_path: Path,
    deleted_event_id: int,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    for index in range(1, 4):
        audit.append(
            event_type="step",
            entity_type="task",
            entity_id="task-1",
            payload={"index": index},
        )

    with store.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE event_id = ?",
            (deleted_event_id,),
        )

    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()
    with pytest.raises(AuditIntegrityError):
        audit.append(
            event_type="blocked",
            entity_type="task",
            entity_id="task-1",
        )


def test_audit_detects_unsealed_writer_after_integrity_activation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="sealed",
        entity_type="task",
        entity_id="task-1",
    )

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events("
            "event_type, entity_type, entity_id, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "bypass",
                "task",
                "task-1",
                "{}",
                "2026-08-23T00:00:00+00:00",
            ),
        )

    with pytest.raises(AuditIntegrityError, match="unsealed after integrity activation"):
        audit.verify_integrity()


def test_audit_anchors_legacy_prefix_without_rewriting_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    legacy_payload = '{"legacy":true}'

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events("
            "event_type, entity_type, entity_id, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "legacy",
                "task",
                "task-1",
                legacy_payload,
                "2026-08-22T00:00:00+00:00",
            ),
        )

    before = audit.verify_integrity()
    assert before.event_count == 1
    assert before.legacy_event_count == 1
    assert before.sealed_event_count == 0
    assert before.integrity_active is False

    audit.append(
        event_type="sealed",
        entity_type="task",
        entity_id="task-1",
        payload={"new": True},
    )
    after = audit.verify_integrity()
    assert after.event_count == 2
    assert after.legacy_event_count == 1
    assert after.sealed_event_count == 1
    assert after.integrity_active is True

    with store.connection() as conn:
        first_payload = str(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )
    assert first_payload == legacy_payload
    assert _INTEGRITY_KEY not in first_payload


def test_sealed_event_detects_later_legacy_prefix_tamper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events("
            "event_type, entity_type, entity_id, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "legacy",
                "task",
                "task-1",
                '{"value":1}',
                "2026-08-22T00:00:00+00:00",
            ),
        )
    audit.append(
        event_type="sealed",
        entity_type="task",
        entity_id="task-1",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 1",
            ('{"value":2}',),
        )

    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()


def test_append_with_connection_rolls_back_with_outer_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    with pytest.raises(RuntimeError, match="force rollback"):
        with store.connection() as conn:
            audit.append_with_connection(
                conn,
                event_type="transient",
                entity_type="task",
                entity_id="task-1",
            )
            raise RuntimeError("force rollback")

    assert audit.verify_integrity().event_count == 0


def test_failed_append_savepoint_cannot_be_committed_by_caller(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="sealed",
        entity_type="task",
        entity_id="task-1",
        payload={"value": 1},
    )
    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )
        raw["value"] = 2
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 1",
            (json.dumps(raw),),
        )

    with store.connection() as conn:
        with pytest.raises(AuditIntegrityError):
            audit.append_with_connection(
                conn,
                event_type="must-not-commit",
                entity_type="task",
                entity_id="task-2",
            )
        # The caller deliberately catches the error; the savepoint still removes
        # the provisional row before this outer transaction commits.
        assert int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]) == 1

    with store.connection() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        sequence = int(
            conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'audit_events'"
            ).fetchone()[0]
        )
    assert count == 1
    assert sequence == 1


def test_concurrent_audit_writers_serialize_into_one_valid_chain(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    def append_one(index: int) -> int:
        return audit.append(
            event_type="concurrent",
            entity_type="worker",
            entity_id=f"worker-{index}",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        event_ids = list(executor.map(append_one, range(6)))

    assert sorted(event_ids) == [1, 2, 3, 4, 5, 6]
    report = audit.verify_integrity()
    assert report.event_count == 6
    assert report.sealed_event_count == 6
    assert report.legacy_event_count == 0


def test_invalid_audit_json_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="created",
        entity_type="task",
        entity_id="task-1",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 1",
            ("{invalid-json",),
        )

    with pytest.raises(AuditIntegrityError, match="not valid JSON"):
        audit.verify_integrity()
