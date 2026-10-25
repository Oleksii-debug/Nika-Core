from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog

_REDACTED = "[REDACTED]"


@pytest.mark.parametrize("credential", ["Z", "QZXWVJK"])
def test_audit_redacts_short_standalone_bearer_values(
    tmp_path: Path,
    credential: str,
) -> None:
    store = SQLiteStore(tmp_path / f"short-bearer-{len(credential)}.db")
    store.initialize()
    audit = AuditLog(store)

    audit.append(
        event_type="security.short_bearer_redaction",
        entity_type="security-test",
        entity_id=f"short-bearer-{len(credential)}",
        payload={"message": f"worker diagnostic Bearer {credential}"},
    )

    event = audit.list_for(
        entity_type="security-test",
        entity_id=f"short-bearer-{len(credential)}",
    )[0]
    sanitized = str(event.payload["message"])
    assert sanitized == f"worker diagnostic Bearer {_REDACTED}"
    assert credential not in sanitized

    with store.connection() as conn:
        raw_payload = str(
            conn.execute(
                "SELECT payload_json FROM audit_events WHERE event_id = 1"
            ).fetchone()["payload_json"]
        )

    assert credential not in raw_payload
    audit.verify_integrity()
