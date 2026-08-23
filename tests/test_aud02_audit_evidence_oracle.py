from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nika_core.kernel.audit as audit_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditIntegrityError, AuditLog

_INTEGRITY_KEY = "_nika_audit_integrity"
_REDACTED = "[REDACTED]"


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "aud02.db")
    store.initialize()
    return store


def _append_pair(audit: AuditLog) -> None:
    audit.append(
        event_type="first",
        entity_type="task",
        entity_id="task-1",
        payload={"value": 1},
    )
    audit.append(
        event_type="second",
        entity_type="task",
        entity_id="task-1",
        payload={"value": 2},
    )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("event_type", "tampered-type"),
        ("entity_type", "tampered-entity-type"),
        ("entity_id", "tampered-entity-id"),
        ("created_at", "2000-01-01T00:00:00+00:00"),
    ],
)
def test_aud02_oracle_chain_binds_event_identity_and_timestamp(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    _append_pair(audit)

    with store.connection() as conn:
        conn.execute(
            f"UPDATE audit_events SET {column} = ? WHERE event_id = 1",
            (replacement,),
        )

    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()


def test_aud02_oracle_chain_binds_event_id_payload_and_previous_digest(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    _append_pair(audit)

    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 2"
            ).fetchone()["payload_json"]
        )
        raw[_INTEGRITY_KEY]["previous_sha256"] = "a" * 64
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 2",
            (json.dumps(raw),),
        )
    with pytest.raises(AuditIntegrityError, match="previous digest"):
        audit.verify_integrity()

    event_id_dir = tmp_path / "event-id"
    event_id_dir.mkdir()
    store = _store(event_id_dir)
    audit = AuditLog(store)
    audit.append(event_type="one", entity_type="task", entity_id="task-1")
    with store.connection() as conn:
        conn.execute("UPDATE audit_events SET event_id = 2 WHERE event_id = 1")
    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()

    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    store = _store(payload_dir)
    audit = AuditLog(store)
    audit.append(
        event_type="one",
        entity_type="task",
        entity_id="task-1",
        payload={"value": "original"},
    )
    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )
        raw["value"] = "tampered"
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 1",
            (json.dumps(raw),),
        )
    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()


@pytest.mark.parametrize("deleted_event_id", [2, 3])
def test_aud02_oracle_detects_middle_and_tail_deletion(
    tmp_path: Path,
    deleted_event_id: int,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    for index in range(1, 4):
        audit.append(
            event_type=f"event-{index}",
            entity_type="task",
            entity_id="task-1",
        )

    with store.connection() as conn:
        conn.execute("DELETE FROM audit_events WHERE event_id = ?", (deleted_event_id,))

    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()


def test_aud02_oracle_legacy_prefix_is_anchored_by_first_sealed_event(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        for index in range(1, 3):
            conn.execute(
                "INSERT INTO audit_events("
                "event_type, entity_type, entity_id, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (f"legacy-{index}", "task", "task-1", "{}", f"legacy-{index}"),
            )

    audit = AuditLog(store)
    before = audit.verify_integrity()
    assert before.legacy_event_count == 2
    assert before.sealed_event_count == 0

    assert audit.append(event_type="sealed", entity_type="task", entity_id="task-1") == 3
    after = audit.verify_integrity()
    assert after.legacy_event_count == 2
    assert after.sealed_event_count == 1

    with store.connection() as conn:
        conn.execute("UPDATE audit_events SET event_type = 'changed' WHERE event_id = 1")
    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()


def test_aud02_oracle_secret_variants_never_reach_raw_sqlite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    secrets = {
        "access_token": "snake-secret",
        "accessToken": "camel-secret",
        "APIKey": "acronym-secret",
        "private.key": "punctuation-secret",
        "requestAuthorization": "authorization-secret",
        "browserCookie": "cookie-secret",
        "userSession": "session-secret",
        "dbPasswd": "passwd-secret",
        "httpCookieHeader": "cookie-header-secret",
    }
    audit.append(
        event_type="redaction",
        entity_type="security",
        entity_id="oracle",
        payload={**secrets, "tokenCount": 7, "sessionCount": 4},
    )

    event = audit.list_for(entity_type="security", entity_id="oracle")[0]
    assert set(event.payload) == {*secrets, "tokenCount", "sessionCount"}
    for key in secrets:
        assert event.payload[key] == _REDACTED
    assert event.payload["tokenCount"] == 7
    assert event.payload["sessionCount"] == 4

    with store.connection() as conn:
        raw = str(conn.execute("SELECT payload_json FROM audit_events").fetchone()[0])
    for secret in secrets.values():
        assert secret not in raw


def test_aud02_oracle_failed_seal_rolls_back_even_when_caller_commits(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    with store.connection() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_seal
            BEFORE UPDATE OF payload_json ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'aud02 seal rejection');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="aud02 seal rejection"):
            audit.append_with_connection(
                conn,
                event_type="must-not-survive",
                entity_type="task",
                entity_id="task-1",
            )
        assert int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]) == 0

    with store.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]) == 0


def test_aud02_oracle_concurrent_writers_are_contiguous(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    def append(index: int) -> int:
        return audit.append(
            event_type="concurrent",
            entity_type="task",
            entity_id=f"task-{index}",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = sorted(executor.map(append, range(8)))

    assert ids == list(range(1, 9))
    report = audit.verify_integrity()
    assert report.event_count == 8
    assert report.sealed_event_count == 8


@pytest.mark.parametrize(
    ("corruption_sql", "parameters"),
    [
        (
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 1",
            ('{"bad":NaN}',),
        ),
        (
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'audit_events'",
            ("not-an-integer",),
        ),
    ],
)
def test_aud02_oracle_invalid_json_and_corrupt_sequence_fail_typed(
    tmp_path: Path,
    corruption_sql: str,
    parameters: tuple[object, ...],
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(event_type="one", entity_type="task", entity_id="task-1")
    with store.connection() as conn:
        conn.execute(corruption_sql, parameters)

    with pytest.raises(AuditIntegrityError):
        audit.verify_integrity()


def test_aud02_oracle_list_read_cannot_cross_verified_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
    audit = AuditLog(store)
    audit.append(
        event_type="before",
        entity_type="task",
        entity_id="task-1",
        payload={"index": 1},
    )

    verified = threading.Event()
    writer_done = threading.Event()
    original_verify = audit_module._verify_connection

    def pause_after_verify(
        conn: sqlite3.Connection,
    ) -> audit_module.AuditIntegrityReport:
        report = original_verify(conn)
        verified.set()
        assert writer_done.wait(timeout=5)
        return report

    monkeypatch.setattr(audit_module, "_verify_connection", pause_after_verify)

    def concurrent_append() -> int:
        assert verified.wait(timeout=5)
        try:
            return AuditLog(store).append(
                event_type="after",
                entity_type="task",
                entity_id="task-1",
                payload={"index": 2},
            )
        finally:
            writer_done.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(concurrent_append)
        observed = audit.list_for(entity_type="task", entity_id="task-1")
        assert future.result(timeout=5) == 2

    assert [event.payload["index"] for event in observed] == [1]
