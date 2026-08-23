from __future__ import annotations

from pathlib import Path

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
