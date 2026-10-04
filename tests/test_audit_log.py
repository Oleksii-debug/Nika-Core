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
            "credential_handle": "opaque-authority-handle",
            "resume_token": "resume-authority",
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
    assert event.payload["credential_handle"] == "[REDACTED]"
    assert event.payload["resume_token"] == "[REDACTED]"
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
                "https://user:pass@example.test/api?mode=safe&code=oauth-secret#details"
                " and will retry"
            )
        },
    )

    message = log.inspect()[0].payload["message"]
    assert isinstance(message, str)
    assert "user:pass" not in message
    assert "oauth-secret" not in message
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

@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://s.example/file?X-Amz-Signature=aws-secret&mode=safe", "aws-secret"),
        ("https://s.example/file?X-Amz-Credential=aws-cred", "aws-cred"),
        ("https://s.example/file?X-Amz-Security-Token=aws-session", "aws-session"),
        ("https://s.example/file?X-Goog-Signature=gcp-secret", "gcp-secret"),
        ("https://s.example/file?X-Goog-Credential=gcp-cred", "gcp-cred"),
        ("https://s.example/file?sig=azure-secret&mode=safe", "azure-secret"),
        ("https://s.example/file?Signature=cdn-secret", "cdn-secret"),
        ("https://s.example/file?X-Amz%2dSignature=encoded-key", "encoded-key"),
    ],
)
def test_signed_storage_urls_are_minimized_in_inspection(tmp_path, url, secret):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="upload.failed",
        entity_type="task",
        entity_id="signed-url",
        payload={"url": url},
    )
    safe = log.inspect()[0].payload["url"]
    assert isinstance(safe, str)
    assert secret not in safe
    assert "REDACTED" in safe
    if "mode=safe" in url:
        assert "mode=safe" in safe


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_percent_encoded_signed_url_fails_closed(tmp_path, layers):
    from urllib.parse import quote

    _, log = _make_log(tmp_path)
    url = "https://s.example/file?X-Amz-Signature=encoded-secret"
    for _ in range(layers):
        url = quote(url, safe="")
    log.append(
        event_type="upload.failed",
        entity_type="task",
        entity_id="encoded-signed-url",
        payload={"message": "source: " + url},
    )
    safe = log.inspect()[0].payload["message"]
    assert isinstance(safe, str)
    assert "encoded-secret" not in safe
    assert "[REDACTED_URL]" in safe


def test_nested_signed_url_is_hidden_but_safe_links_are_preserved(tmp_path):
    from urllib.parse import quote

    _, log = _make_log(tmp_path)
    nested = "https://storage.example/file?X-Goog-Signature=nested-secret"
    url = "https://service.example/redirect?next=" + quote(nested, safe="")
    plain = "https://service.example/health?mode=safe"
    log.append(
        event_type="upload.failed",
        entity_type="task",
        entity_id="nested-url",
        payload={"target": url, "health": plain},
    )
    result = log.inspect()[0].payload
    assert "nested-secret" not in result["target"]
    assert "REDACTED_URL" in result["target"]
    assert result["health"] == plain

@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Signature=cloudfront-secret", "cloudfront-secret"),
        ("sig=azure-secret", "azure-secret"),
        ("X-Amz-Credential=aws-secret", "aws-secret"),
        ("X-Goog-Signature=gcp-secret", "gcp-secret"),
        ("X-Amz-Security-Token=aws-session", "aws-session"),
    ],
)
def test_inline_signed_parameters_are_not_exposed(tmp_path, message, secret):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="provider.failed",
        entity_type="task",
        entity_id="inline-signed",
        payload={
            "message": "failed with " + message,
            "credential_id": "public-reference",
            "signature_status": "verified",
        },
    )
    event = log.inspect()[0]
    assert secret not in event.payload["message"]
    assert "[REDACTED]" in event.payload["message"]
    assert event.payload["credential_id"] == "public-reference"
    assert event.payload["signature_status"] == "verified"

@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), -float("inf")])
def test_append_rejects_noncanonical_json_number_before_persistence(tmp_path, nonfinite):
    _, log = _make_log(tmp_path)
    with pytest.raises(ValueError, match="Out of range float"):
        log.append(
            event_type="provider.failed",
            entity_type="task",
            entity_id="invalid-numeric",
            payload={"nested": [{"duration": nonfinite}]},
        )
    assert log.inspect() == ()


@pytest.mark.parametrize(
    "corrupted",
    [
        '{"duration":NaN}',
        '{"duration":Infinity}',
        '{"duration":-Infinity}',
        '{"mode":"denied","mode":"granted"}',
        '{"nested":{"approved":false,"approved":false}}',
    ],
)
def test_inspection_rejects_noncanonical_persisted_json(tmp_path, corrupted):
    store, log = _make_log(tmp_path)
    event_id = log.append(
        event_type="task.created",
        entity_type="task",
        entity_id="corrupt-json",
        payload={"ok": True},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (corrupted, event_id),
        )
    with pytest.raises(AuditIntegrityError, match=f"audit event {event_id}"):
        log.inspect()
    with pytest.raises(AuditIntegrityError, match=f"audit event {event_id}"):
        log.list_for(entity_type="task", entity_id="corrupt-json")

@pytest.mark.parametrize("corrupted", ['{"value":1e9999}', '{"value":-1e9999}'])
def test_inspection_rejects_finite_syntax_that_overflows_float(tmp_path, corrupted):
    store, log = _make_log(tmp_path)
    event_id = log.append(
        event_type="task.created",
        entity_type="task",
        entity_id="overflow-json",
        payload={"value": 1e308},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (corrupted, event_id),
        )
    with pytest.raises(AuditIntegrityError, match=f"audit event {event_id}"):
        log.inspect()


def test_non_object_payload_is_not_silently_converted_to_empty_object(tmp_path):
    _, log = _make_log(tmp_path)
    with pytest.raises(TypeError, match="JSON object"):
        log.append(
            event_type="task.created",
            entity_type="task",
            entity_id="nonobject",
            payload=[],
        )
    assert log.inspect() == ()


def test_nonfinite_payload_rolls_back_earlier_audit_in_same_transaction(tmp_path):
    store, log = _make_log(tmp_path)
    with pytest.raises(ValueError, match="Out of range float"):
        with store.connection() as conn:
            log.append_with_connection(
                conn,
                event_type="task.started",
                entity_type="task",
                entity_id="rollback",
                payload={"status": "running"},
            )
            log.append_with_connection(
                conn,
                event_type="task.completed",
                entity_type="task",
                entity_id="rollback",
                payload={"score": float("nan")},
            )
    assert log.list_for(entity_type="task", entity_id="rollback") == ()


def test_canonical_finite_json_number_round_trips(tmp_path):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="task.completed",
        entity_type="task",
        entity_id="finite",
        payload={"score": 1e308},
    )
    assert log.inspect()[0].payload == {"score": 1e308}

@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://s.example/file?X-Amz%252dSignature=double-secret", "double-secret"),
        ("https://s.example/file?X-Amz%25252dSignature=triple-secret", "triple-secret"),
        ("https://user%3Apass%40s.example/path", "pass"),
        ("https://user%253Apass%2540s.example/path", "pass"),
        ("https://s.example/?mode=safe%26sig%3Dseparator-secret", "separator-secret"),
        ("https://s.example/?mode=safe%253Bsig%253Dnested-separator", "nested-separator"),
    ],
)
def test_encoded_signed_url_authority_never_reaches_inspection(tmp_path, url, secret):
    _, log = _make_log(tmp_path)
    log.append(
        event_type="provider.failed",
        entity_type="task",
        entity_id="encoded-authority",
        payload={"url": url},
    )
    safe = log.inspect()[0].payload["url"]
    assert secret not in safe
    assert "REDACTED" in safe
