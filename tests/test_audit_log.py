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
    ("kwargs", "error_type", "message"),
    [
        ({"limit": 0}, ValueError, "limit"),
        ({"limit": 501}, ValueError, "limit"),
        ({"after_event_id": -1}, ValueError, "after_event_id"),
        ({"event_type": " "}, ValueError, "event_type"),
        ({"entity_type": ""}, ValueError, "entity_type"),
        ({"entity_id": "\t"}, ValueError, "entity_id"),
        ({"limit": True}, TypeError, "limit"),
        ({"limit": 1.5}, TypeError, "limit"),
        ({"after_event_id": False}, TypeError, "after_event_id"),
        ({"event_type": 42}, TypeError, "event_type"),
    ],
)
def test_inspection_query_rejects_invalid_inputs(kwargs, error_type, message):
    with pytest.raises(error_type, match=message):
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
                    "token=query-secret&mode=safe#access_token=fragment-secret"
                ),
            },
            "messages": [
                "Authorization: Bearer super-secret",
                "Cookie: sessionid=cookie-secret",
                "password=hunter2 failed",
                "token=loose-secret failed",
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
    assert "fragment-secret" not in endpoint
    assert "mode=safe" in endpoint

    messages = event.payload["messages"]
    assert isinstance(messages, list)
    assert messages[0] == "Authorization: [REDACTED]"
    assert messages[1] == "Cookie: [REDACTED]"
    assert messages[2] == "password=[REDACTED] failed"
    assert messages[3] == "token=[REDACTED] failed"
    assert messages[4] == "plain text"


def test_embedded_url_credentials_are_redacted_without_destroying_safe_fragment(tmp_path):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="provider.failed",
        entity_type="task",
        entity_id="task-embedded-url",
        payload={
            "message": (
                "request failed at "
                "https://user:pass@example.test/api?mode=safe&token=secret#details"
                " and will retry"
            )
        },
    )

    message = log.inspect()[0].payload["message"]
    assert isinstance(message, str)
    assert "user:pass" not in message
    assert "secret" not in message
    assert "mode=safe" in message
    assert "#details" in message
    assert "and will retry" in message


def test_malformed_web_url_fails_closed_instead_of_returning_userinfo(tmp_path):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="provider.failed",
        entity_type="task",
        entity_id="task-malformed-url",
        payload={
            "endpoint": "https://user:pass@example.test:not-a-port/path?token=secret",
        },
    )

    event = log.inspect()[0]
    assert event.payload["endpoint"] == "[REDACTED_URL]"


def test_private_key_block_is_not_repeated_into_inspection(tmp_path):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="provider.failed",
        entity_type="task",
        entity_id="task-private-key",
        payload={
            "message": (
                "load failed -----BEGIN PRIVATE KEY-----\nsecret-material\n"
                "-----END PRIVATE KEY----- during startup"
            )
        },
    )

    message = log.inspect()[0].payload["message"]
    assert isinstance(message, str)
    assert "secret-material" not in message
    assert "PRIVATE KEY" not in message
    assert "[REDACTED]" in message


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
