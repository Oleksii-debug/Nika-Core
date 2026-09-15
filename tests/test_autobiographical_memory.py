from __future__ import annotations

import json

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


def _runtime(tmp_path):
    store = SQLiteStore(tmp_path / "ніка memory" / "nika.sqlite3")
    store.initialize()
    audit = AuditLog(store)
    memory = MemoryService(store, audit=audit)
    autobiography = AutobiographicalMemory(store, memory)
    return store, audit, memory, autobiography


def test_remembered_evidence_survives_restart_without_copying_payload(tmp_path) -> None:
    store, audit, _, autobiography = _runtime(tmp_path)
    event_id = audit.append(
        event_type="learning.lesson.accepted",
        entity_type="task",
        entity_id="task-1",
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

    memory_events = audit.list_for(
        entity_type="memory",
        entity_id=f"agent:agent-1:autobiography:lesson:{event_id}",
    )
    assert [event.event_type for event in memory_events] == ["memory.upserted"]

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_memory = MemoryService(restarted_store, audit=AuditLog(restarted_store))
    restarted = AutobiographicalMemory(restarted_store, restarted_memory)

    assert restarted.list_entries(agent_id="agent-1") == (first,)


def test_same_audit_event_can_support_distinct_typed_memories(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = audit.append(
        event_type="experiment.completed",
        entity_type="experiment",
        entity_id="exp-1",
        payload={"status": "rejected"},
    )

    lesson = autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.LESSON,
        audit_event_id=event_id,
    )
    mistake = autobiography.remember_audit_event(
        agent_id="agent-1",
        category=AutobiographicalCategory.MISTAKE,
        audit_event_id=event_id,
    )

    assert lesson.audit_event_id == mistake.audit_event_id == event_id
    assert lesson.audit_event_sha256 == mistake.audit_event_sha256
    assert autobiography.list_entries(agent_id="agent-1") == (lesson, mistake)


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

    event_id = audit.append(
        event_type="task.completed",
        entity_type="task",
        entity_id="task-1",
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
            ('{"result":"changed"}', event_id),
        )

    with pytest.raises(
        AutobiographicalMemoryIntegrityError,
        match="evidence changed after it was remembered",
    ):
        autobiography.list_entries(agent_id="agent-1")


def test_nonfinite_or_noncanonical_audit_payload_fails_closed(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = audit.append(
        event_type="experiment.measured",
        entity_type="experiment",
        entity_id="exp-2",
        payload={"score": float("nan")},
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
    event_id = audit.append(
        event_type="decision.recorded",
        entity_type="task",
        entity_id="task-2",
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
        autobiography.list_entries(agent_id="agent-1")


def test_forget_is_explicit_and_agent_scoped(tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = audit.append(
        event_type="procedure.verified",
        entity_type="task",
        entity_id="task-3",
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
    assert autobiography.list_entries(agent_id="agent-1") == ()


@pytest.mark.parametrize("agent_id", ["", "agent/one", "agent one", "agent\nadmin"])
def test_agent_id_is_a_bounded_machine_token(agent_id: str, tmp_path) -> None:
    _, audit, _, autobiography = _runtime(tmp_path)
    event_id = audit.append(
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
    event_id = audit.append(
        event_type="lesson.generated",
        entity_type="task",
        entity_id="task-4",
    )

    with pytest.raises(AutobiographicalMemoryError, match="category"):
        autobiography.remember_audit_event(
            agent_id="agent-1",
            category="lesson",
            audit_event_id=event_id,
        )
