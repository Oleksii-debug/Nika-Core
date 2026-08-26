from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditIntegrityError, AuditInspectionQuery, AuditLog


def _make_log(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store, AuditLog(store)


def test_inspect_filters_orders_and_pages(tmp_path):
    _, log = _make_log(tmp_path)
    first = log.append(
        event_type="task.created",
        entity_type="task",
        entity_id="task-1",
        payload={"status": "queued"},
    )
    second = log.append(
        event_type="task.started",
        entity_type="task",
        entity_id="task-1",
        payload={"status": "running"},
    )
    log.append(
        event_type="task.created",
        entity_type="task",
        entity_id="task-2",
        payload={"status": "queued"},
    )

    page = log.inspect(AuditInspectionQuery(entity_type="task", entity_id="task-1", limit=1))
    assert [event.event_id for event in page] == [first]
    assert page[0].payload == {"status": "queued"}

    next_page = log.inspect(
        AuditInspectionQuery(
            entity_type="task",
            entity_id="task-1",
            after_event_id=page[-1].event_id,
            limit=10,
        )
    )
    assert [event.event_id for event in next_page] == [second]

    created = log.inspect(AuditInspectionQuery(event_type="task.created"))
    assert [event.entity_id for event in created] == ["task-1", "task-2"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 501}, "limit"),
        ({"after_event_id": -1}, "after_event_id"),
        ({"event_type": " "}, "event_type"),
        ({"entity_type": ""}, "entity_type"),
        ({"entity_id": "\t"}, "entity_id"),
    ],
)
def test_inspection_query_rejects_unbounded_or_blank_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        AuditInspectionQuery(**kwargs)


def test_inspect_redacts_nested_credentials_and_url_secrets(tmp_path):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="provider.failed",
        entity_type="task",
        entity_id="task-secret",
        payload={
            "api_key": "sk-secret",
            "credential_id": "cred-safe-ref",
            "nested": {
                "refresh_token": "refresh-secret",
                "endpoint": (
                    "https://user:pass@example.test/path?"
                    "token=query-secret&mode=safe"
                ),
            },
            "messages": [
                "Authorization: Bearer super-secret",
                "password=hunter2 failed",
                "plain text",
            ],
        },
    )

    event = log.inspect()[0]
    assert event.payload["api_key"] == "[REDACTED]"
    assert event.payload["credential_id"] == "cred-safe-ref"
    nested = event.payload["nested"]
    assert isinstance(nested, dict)
    assert nested["refresh_token"] == "[REDACTED]"
    endpoint = nested["endpoint"]
    assert isinstance(endpoint, str)
    assert "user:pass" not in endpoint
    assert "query-secret" not in endpoint
    assert "mode=safe" in endpoint

    messages = event.payload["messages"]
    assert isinstance(messages, list)
    assert messages[0] == "Authorization: [REDACTED]"
    assert messages[1] == "password=[REDACTED] failed"
    assert messages[2] == "plain text"


def test_existing_list_for_keeps_raw_payload_contract(tmp_path):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="credential.used",
        entity_type="task",
        entity_id="task-raw",
        payload={"token": "internal-value"},
    )

    raw = log.list_for(entity_type="task", entity_id="task-raw")
    inspected = log.inspect(AuditInspectionQuery(entity_id="task-raw"))

    assert raw[0].payload["token"] == "internal-value"
    assert inspected[0].payload["token"] == "[REDACTED]"


def test_inspect_fails_closed_on_corrupt_payload(tmp_path):
    store, log = _make_log(tmp_path)
    event_id = log.append(
        event_type="task.created",
        entity_type="task",
        entity_id="task-corrupt",
        payload={"ok": True},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            ("not-json", event_id),
        )

    with pytest.raises(AuditIntegrityError, match=f"audit event {event_id}"):
        log.inspect()
