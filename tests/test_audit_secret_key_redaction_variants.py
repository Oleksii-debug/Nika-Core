from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog

_REDACTED = "[REDACTED]"


def test_audit_redacts_camel_acronym_and_punctuation_secret_keys(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    secrets = {
        "accessToken": "camel-access-token-must-not-persist",
        "APIKey": "acronym-api-key-must-not-persist",
        "private.key": "punctuated-private-key-must-not-persist",
        "clientAPIKey": "suffixed-api-key-must-not-persist",
        "nestedRefreshToken": "nested-refresh-token-must-not-persist",
    }

    audit.append(
        event_type="security.secret_redaction",
        entity_type="security-test",
        entity_id="variant-keys",
        payload={
            "accessToken": secrets["accessToken"],
            "APIKey": secrets["APIKey"],
            "private.key": secrets["private.key"],
            "clientAPIKey": secrets["clientAPIKey"],
            "nested": {"nestedRefreshToken": secrets["nestedRefreshToken"]},
            "tokenCount": 7,
            "safeLabel": "visible",
        },
    )

    event = audit.list_for(
        entity_type="security-test",
        entity_id="variant-keys",
    )[0]
    assert event.payload == {
        "accessToken": _REDACTED,
        "APIKey": _REDACTED,
        "private.key": _REDACTED,
        "clientAPIKey": _REDACTED,
        "nested": {"nestedRefreshToken": _REDACTED},
        "tokenCount": 7,
        "safeLabel": "visible",
    }

    with store.connection() as conn:
        raw_payload = str(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )

    for secret in secrets.values():
        assert secret not in raw_payload
    assert "visible" in raw_payload
    audit.verify_integrity()


@pytest.mark.parametrize(
    ("field", "value", "canaries"),
    [
        (
            "error",
            "request failed with Authorization: Bearer VALUE_CANARY_AUTH_71C4",
            ("VALUE_CANARY_AUTH_71C4",),
        ),
        (
            "detail",
            "provider rejected api_key=VALUE_CANARY_PROVIDER_2A6E",
            ("VALUE_CANARY_PROVIDER_2A6E",),
        ),
        (
            "url",
            "https://audit-user:VALUE_CANARY_URL_PASSWORD_84D1@audit.invalid/path"
            "?access_token=VALUE_CANARY_QUERY_TOKEN_63B9&page=1",
            ("VALUE_CANARY_URL_PASSWORD_84D1", "VALUE_CANARY_QUERY_TOKEN_63B9"),
        ),
        (
            "stderr",
            "fatal: repository 'https://oauth2:VALUE_CANARY_GIT_TOKEN_5F20@"
            "github.invalid/org/repo.git/' not found",
            ("VALUE_CANARY_GIT_TOKEN_5F20",),
        ),
        (
            "message",
            "Cookie: sessionid=VALUE_CANARY_COOKIE_9BE3",
            ("VALUE_CANARY_COOKIE_9BE3",),
        ),
        (
            "message",
            "retry used Bearer VALUE_CANARY_STANDALONE_BEARER_47AA",
            ("VALUE_CANARY_STANDALONE_BEARER_47AA",),
        ),
    ],
)
def test_audit_redacts_secret_material_embedded_in_benign_string_values(
    tmp_path: Path,
    field: str,
    value: str,
    canaries: tuple[str, ...],
) -> None:
    store = SQLiteStore(tmp_path / "embedded-values.db")
    store.initialize()
    audit = AuditLog(store)

    audit.append(
        event_type="security.secret_value_redaction",
        entity_type="security-test",
        entity_id="embedded-values",
        payload={field: value},
    )

    event = audit.list_for(
        entity_type="security-test",
        entity_id="embedded-values",
    )[0]
    sanitized = str(event.payload[field])
    assert _REDACTED in sanitized

    with store.connection() as conn:
        raw_payload = str(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )

    for canary in canaries:
        assert canary not in sanitized
        assert canary not in raw_payload
    audit.verify_integrity()


def test_audit_string_redaction_preserves_count_like_non_secret_metadata(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "count-values.db")
    store.initialize()
    audit = AuditLog(store)
    diagnostic = "tokenCount=7 sessionCount=4 cookieCount=2"

    audit.append(
        event_type="security.safe_counts",
        entity_type="security-test",
        entity_id="count-values",
        payload={"message": diagnostic},
    )

    event = audit.list_for(
        entity_type="security-test",
        entity_id="count-values",
    )[0]
    assert event.payload["message"] == diagnostic
    audit.verify_integrity()
