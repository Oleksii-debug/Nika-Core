from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.autobiographical_memory import (
    AutobiographicalCategory,
    AutobiographicalMemory,
    AutobiographicalMemoryError,
    AutobiographicalMemoryIntegrityError,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory.service import MemoryService


class _CountingSQLiteStore(SQLiteStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.audit_selects = 0
        self.memory_selects = 0

    @contextmanager
    def connection(self):
        with super().connection() as conn:
            def trace(statement: str) -> None:
                sql = " ".join(statement.lower().split())
                if sql.startswith("select") and " from audit_events " in f" {sql} ":
                    self.audit_selects += 1
                if sql.startswith("select") and " from memory_records " in f" {sql} ":
                    self.memory_selects += 1

            conn.set_trace_callback(trace)
            yield conn


def _runtime(tmp_path, *, counting: bool = False):
    store_type = _CountingSQLiteStore if counting else SQLiteStore
    store = store_type(tmp_path / "ніка memory" / "nika.sqlite3")
    store.initialize()
    audit = AuditLog(store)
    memory = MemoryService(store, audit=audit)
    autobiography = AutobiographicalMemory(store, memory)
    return store, audit, memory, autobiography


def _append_attested(
    audit: AuditLog,
    *,
    category: AutobiographicalCategory,
    event_type: str,
    entity_type: str = "task",
    entity_id: str = "task-1",
    agent_id: str = "agent-1",
    payload: dict[str, object] | None = None,
) -> int:
    body = dict(payload or {})
    body["autobiographical_category"] = category.value
    body["autobiographical_agent_id"] = agent_id
    return audit.append(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=body,
    )


def test_remembered_evidence_survives_restart_without_copying_payload(tmp_path) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.LESSON,
        event_type="learning.lesson.accepted",
        payload={
            "outcome": "improved",
            "private_note": "AUTOBIO_SECRET_CANARY",
            "nested": {"path": r"C:\Users\Alice\private\lesson.txt"},
        },
    )

    first = autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.LESSON,
        audit_event_id=event_id,
    )
    repeated = autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.LESSON,
        audit_event_id=event_id,
    )

    assert repeated == first
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records "
            "WHERE scope = 'agent' AND owner_id = ? AND namespace = 'autobiography'",
            ("agent-1",),
        ).fetchone()
    assert row is not None
    raw_value = row["value_json"]
    assert "AUTOBIO_SECRET_CANARY" not in raw_value
    assert "Alice" not in raw_value
    assert "learning.lesson.accepted" not in raw_value
    assert "task-1" not in raw_value
    assert '"agent_id":"agent-1"' in raw_value

    memory_events = audit.list_for(
        entity_type="memory",
        entity_id=f"agent:agent-1:autobiography:lesson:{event_id}",
    )
    assert [event.event_type for event in memory_events] == ["memory.upserted"]

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_memory = MemoryService(restarted_store, audit=AuditLog(restarted_store))
    restarted = AutobiographicalMemory(restarted_store, restarted_memory)

    assert restarted.list_entries(agent_id="agent-1", limit=10) == (first,)


def test_category_must_be_attested_by_referenced_audit_evidence(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    unrelated = audit.append(
        event_type="task.created",
        entity_type="task",
        entity_id="task-1",
        payload={"status": "created"},
    )

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="lacks autobiographical category attestation",
    ):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.LESSON,
            audit_event_id=unrelated,
        )

    outcome = _append_attested(
        audit,
        category=AutobiographicalCategory.OUTCOME,
        event_type="task.completed",
        payload={"status": "done"},
    )
    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="does not attest requested autobiographical category",
    ):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.LESSON,
            audit_event_id=outcome,
        )

    remembered = autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.OUTCOME,
        audit_event_id=outcome,
    )
    assert remembered.category is AutobiographicalCategory.OUTCOME


def test_audit_evidence_cannot_be_rebound_to_another_agent(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.LESSON,
        event_type="learning.lesson.accepted",
        agent_id="agent-1",
    )

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="does not attest requested autobiographical agent",
    ):
        autobiography.remember_audit_event(
            agent_id="agent-2",
            category=AutobiographicalCategory.LESSON,
            audit_event_id=event_id,
        )

    assert autobiography.list_entries(agent_id="agent-2", limit=10) == ()


def test_tampered_audit_agent_attestation_fails_restart_validation(tmp_path) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.DECISION,
        event_type="decision.recorded",
        agent_id="agent-1",
    )
    autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.DECISION,
        audit_event_id=event_id,
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["autobiographical_agent_id"] = "agent-2"
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), event_id),
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = AutobiographicalMemory(
        restarted_store,
        MemoryService(restarted_store, audit=AuditLog(restarted_store)),
    )
    with pytest.raises(AutobiographicalMemoryIntegrityError):
        restarted.list_entries(agent_id="agent-1", limit=10)


def test_owner_rebinding_fails_closed_and_does_not_fabricate_replacement(tmp_path) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.DECISION,
        event_type="decision.recorded",
        payload={"decision": "retry"},
    )
    autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.DECISION,
        audit_event_id=event_id,
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET owner_id = ? WHERE scope = 'agent' "
            "AND owner_id = ? AND namespace = 'autobiography'",
            ("agent-2", "agent-1"),
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = AutobiographicalMemory(
        restarted_store,
        MemoryService(restarted_store, audit=AuditLog(restarted_store)),
    )
    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="owner binding is invalid",
    ):
        restarted.list_entries(agent_id="agent-2", limit=10)
    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="missing after prior persistence",
    ):
        restarted.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.DECISION,
            audit_event_id=event_id,
        )

    with restarted_store.connection() as conn:
        rows = conn.execute(
            "SELECT owner_id, value_json FROM memory_records "
            "WHERE namespace = 'autobiography'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["owner_id"] == "agent-2"
    assert json.loads(rows[0]["value_json"])["agent_id"] == "agent-1"


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime.now(UTC) - timedelta(days=1),
        datetime.now(UTC) + timedelta(days=1),
    ],
)
def test_tampered_expiry_fails_closed_without_silent_purge_or_recreation(
    tmp_path, expires_at: datetime
) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.PROCEDURE,
        event_type="procedure.verified",
        payload={"status": "verified"},
    )
    autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.PROCEDURE,
        audit_event_id=event_id,
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE scope = 'agent' "
            "AND owner_id = ? AND namespace = 'autobiography'",
            (expires_at.isoformat(), "agent-1"),
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = AutobiographicalMemory(
        restarted_store,
        MemoryService(restarted_store, audit=AuditLog(restarted_store)),
    )
    with pytest.raises(AutobiographicalMemoryIntegrityError, match="must not have an expiry"):
        restarted.list_entries(agent_id="agent-1", limit=10)
    with pytest.raises(AutobiographicalMemoryIntegrityError, match="must not have an expiry"):
        restarted.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.PROCEDURE,
            audit_event_id=event_id,
        )

    with restarted_store.connection() as conn:
        row = conn.execute(
            "SELECT expires_at FROM memory_records WHERE scope = 'agent' "
            "AND owner_id = ? AND namespace = 'autobiography'",
            ("agent-1",),
        ).fetchone()
    assert row is not None
    assert row["expires_at"] == expires_at.isoformat()


def test_bounded_recent_read_batches_audit_validation_and_skips_older_corruption(
    tmp_path,
) -> None:
    store, audit, _, autobiography = _runtime(tmp_path, counting=True)
    remembered = []
    event_ids = []
    for index in range(20):
        event_id = _append_attested(
            audit,
            category=AutobiographicalCategory.OUTCOME,
            event_type="task.completed",
            entity_id=f"task-{index}",
            payload={"index": index},
        )
        event_ids.append(event_id)
        remembered.append(
            autobiography.remember_audit_event(
                agent_id="agent-1",
                category=AutobiographicalCategory.OUTCOME,
                audit_event_id=event_id,
            )
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_id = ?",
            (event_ids[0],),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["index"] = "tampered-old-entry"
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), event_ids[0]),
        )

    store.audit_selects = 0
    store.memory_selects = 0
    recent = autobiography.list_entries(agent_id="agent-1", limit=5)
    assert recent == tuple(reversed(remembered[-5:]))
    assert store.memory_selects == 1
    assert store.audit_selects == 1

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="evidence changed after it was remembered",
    ):
        autobiography.list_entries(agent_id="agent-1", limit=20)


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.0])
def test_list_limit_is_strictly_bounded(limit: object, tmp_path) -> None:
    _, _, _, autobiography = _runtime(tmp_path)
    with pytest.raises(AutobiographicalMemoryError, match="limit"):
        autobiography.list_entries(agent_id="agent-1", limit=limit)


def test_missing_or_tampered_audit_evidence_fails_closed(tmp_path) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="audit evidence does not exist",
    ):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.OUTCOME,
            audit_event_id=999,
        )

    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.OUTCOME,
        event_type="task.completed",
        payload={"result": "ok"},
    )
    autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.OUTCOME,
        audit_event_id=event_id,
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (
                (
                    '{"autobiographical_agent_id":"agent-1",'
                    '"autobiographical_category":"outcome","result":"changed"}'
                ),
                event_id,
            ),
        )

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="evidence changed after it was remembered",
    ):
        autobiography.list_entries(agent_id="agent-1", limit=10)


def test_nonfinite_or_noncanonical_audit_payload_fails_closed(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = audit.append(
        event_type="experiment.measured",
        entity_type="experiment",
        entity_id="exp-2",
        payload={
            "autobiographical_category": AutobiographicalCategory.OUTCOME.value,
            "score": float("nan"),
        },
    )

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="unsupported JSON values",
    ):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.OUTCOME,
            audit_event_id=event_id,
        )


def test_tampered_memory_schema_fails_closed(tmp_path) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.DECISION,
        event_type="decision.recorded",
        payload={"decision": "retry"},
    )
    autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.DECISION,
        audit_event_id=event_id,
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records "
            "WHERE scope = 'agent' AND owner_id = ? AND namespace = 'autobiography'",
            ("agent-1",),
        ).fetchone()
        assert row is not None
        value = json.loads(row["value_json"])
        value["unexpected"] = True
        conn.execute(
            "UPDATE memory_records SET value_json = ? "
            "WHERE scope = 'agent' AND owner_id = ? AND namespace = 'autobiography'",
            (
                json.dumps(value, sort_keys=True, separators=(",", ":")),
                "agent-1",
            ),
        )

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="invalid schema",
    ):
        autobiography.list_entries(agent_id="agent-1", limit=10)


def test_forget_is_explicit_agent_scoped_and_allows_later_remember(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.PROCEDURE,
        event_type="procedure.verified",
        payload={"status": "verified"},
    )
    autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.PROCEDURE,
        audit_event_id=event_id,
    )

    assert not autobiography.forget_audit_event(
        agent_id="agent-2",
        category=AutobiographicalCategory.PROCEDURE,
        audit_event_id=event_id,
    )
    assert autobiography.forget_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.PROCEDURE,
        audit_event_id=event_id,
    )
    assert autobiography.list_entries(agent_id="agent-1", limit=10) == ()

    restored = autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.PROCEDURE,
        audit_event_id=event_id,
    )
    assert restored.audit_event_id == event_id


@pytest.mark.parametrize("agent_id", ["", "agent/one", "agent one", "agent\nadmin"])
def test_agent_id_is_a_bounded_machine_token(agent_id: str, tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.REPORT_HISTORY,
        event_type="report.generated",
        entity_type="report",
        entity_id="daily-1",
    )

    with pytest.raises(AutobiographicalMemoryError, match="agent_id"):
        autobiography.remember_audit_event(
            agent_id=agent_id,
            category=AutobiographicalCategory.REPORT_HISTORY,
            audit_event_id=event_id,
        )


@pytest.mark.parametrize("event_id", [True, 0, -1, 1 << 63])
def test_event_id_is_a_positive_signed_64_integer(event_id: object, tmp_path) -> None:
    _, _, _, autobiography = _runtime(tmp_path)

    with pytest.raises(AutobiographicalMemoryError, match="audit_event_id"):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category=AutobiographicalCategory.LESSON,
            audit_event_id=event_id,
        )


def test_category_is_strictly_typed(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = _append_attested(
        audit,
        category=AutobiographicalCategory.LESSON,
        event_type="lesson.generated",
        entity_id="task-4",
    )

    with pytest.raises(AutobiographicalMemoryError, match="category"):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category="lesson",
            audit_event_id=event_id,
        )
