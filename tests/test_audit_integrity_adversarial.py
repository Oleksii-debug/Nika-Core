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

_REDACTED = "[REDACTED]"


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_prefixed_secret_variants_redact_without_count_false_positives(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    secrets = {
        "requestAuthorization": "authz-value-must-not-persist",
        "browserCookie": "cookie-value-must-not-persist",
        "userSession": "session-value-must-not-persist",
        "dbPasswd": "passwd-value-must-not-persist",
        "httpCookieHeader": "cookie-header-value-must-not-persist",
    }

    audit.append(
        event_type="security.secret_redaction",
        entity_type="security-test",
        entity_id="prefixed-variants",
        payload={
            **secrets,
            "tokenCount": 9,
            "sessionCount": 2,
            "cookieCount": 3,
            "safeLabel": "visible",
        },
    )

    event = audit.list_for(
        entity_type="security-test",
        entity_id="prefixed-variants",
    )[0]
    for key in secrets:
        assert event.payload[key] == _REDACTED
    assert event.payload["tokenCount"] == 9
    assert event.payload["sessionCount"] == 2
    assert event.payload["cookieCount"] == 3
    assert event.payload["safeLabel"] == "visible"

    with store.connection() as conn:
        raw_payload = str(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )
    for secret in secrets.values():
        assert secret not in raw_payload


@pytest.mark.parametrize("replacement", ["corrupt", -1, 1.5])
def test_corrupt_sqlite_sequence_is_typed_integrity_error(
    tmp_path: Path,
    replacement: object,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="created",
        entity_type="task",
        entity_id="task-1",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'audit_events'",
            (replacement,),
        )

    with pytest.raises(AuditIntegrityError, match="sequence is invalid"):
        audit.verify_integrity()


@pytest.mark.parametrize("payload_json", ['{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'])
def test_nonfinite_payload_corruption_is_typed_integrity_error(
    tmp_path: Path,
    payload_json: str,
) -> None:
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
            (payload_json,),
        )

    with pytest.raises(AuditIntegrityError, match="not valid JSON"):
        audit.verify_integrity()


def test_failed_sealing_update_cannot_leave_provisional_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)

    with store.connection() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_audit_seal
            BEFORE UPDATE OF payload_json ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'seal rejected');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal rejected"):
            audit.append_with_connection(
                conn,
                event_type="must-not-persist",
                entity_type="task",
                entity_id="task-1",
                payload={"safe": True},
            )

        # The caller catches the sealing failure, so the outer transaction commits.
        assert int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]) == 0
        sequence_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'audit_events'"
        ).fetchone()
        assert sequence_row is None or int(sequence_row[0]) == 0

    with store.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]) == 0


def test_list_for_uses_one_snapshot_for_verification_and_entity_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    with store.connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
    audit.append(
        event_type="first",
        entity_type="task",
        entity_id="task-1",
        payload={"index": 1},
    )

    verified = threading.Event()
    writer_done = threading.Event()
    original_verify = audit_module._verify_connection

    def interleaved_verify(
        conn: sqlite3.Connection,
    ) -> audit_module.AuditIntegrityReport:
        report = original_verify(conn)
        verified.set()
        assert writer_done.wait(timeout=5)
        return report

    monkeypatch.setattr(audit_module, "_verify_connection", interleaved_verify)

    def append_second() -> int:
        assert verified.wait(timeout=5)
        try:
            return AuditLog(store).append(
                event_type="second",
                entity_type="task",
                entity_id="task-1",
                payload={"index": 2},
            )
        finally:
            writer_done.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(append_second)
        events = audit.list_for(entity_type="task", entity_id="task-1")
        assert future.result(timeout=5) == 2

    assert [event.payload["index"] for event in events] == [1]
    monkeypatch.setattr(audit_module, "_verify_connection", original_verify)
    events_after = audit.list_for(entity_type="task", entity_id="task-1")
    assert [event.payload["index"] for event in events_after] == [1, 2]


def test_previous_digest_tamper_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(event_type="first", entity_type="task", entity_id="task-1")
    audit.append(event_type="second", entity_type="task", entity_id="task-1")

    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 2"
            ).fetchone()["payload_json"]
        )
        raw["_nika_audit_integrity"]["previous_sha256"] = "f" * 64
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = 2",
            (json.dumps(raw),),
        )

    with pytest.raises(AuditIntegrityError, match="previous digest does not match"):
        audit.verify_integrity()


def test_event_id_corruption_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(event_type="created", entity_type="task", entity_id="task-1")

    with store.connection() as conn:
        conn.execute("UPDATE audit_events SET event_id = 2 WHERE event_id = 1")

    with pytest.raises(AuditIntegrityError, match="not contiguous"):
        audit.verify_integrity()
