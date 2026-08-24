from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog


@pytest.mark.parametrize(
    ("query_key", "canary"),
    [
        ("token", "QA53_CANARY_QUERY_ALIAS_TOKEN_14C8"),
        ("auth", "QA53_CANARY_QUERY_ALIAS_AUTH_35D1"),
        ("key", "QA53_CANARY_QUERY_ALIAS_KEY_72F4"),
        ("sig", "QA53_CANARY_QUERY_ALIAS_SIG_88A6"),
    ],
)
def test_qa53_generic_audit_redaction_blocks_secret_query_aliases(
    tmp_path: Path,
    query_key: str,
    canary: str,
) -> None:
    store = SQLiteStore(tmp_path / "qa53-query-alias.db")
    store.initialize()
    audit = AuditLog(store)

    audit.append(
        event_type="qa53-query-alias-oracle",
        entity_type="security",
        entity_id="qa53",
        payload={"url": f"https://audit.invalid/redirect?{query_key}={canary}&page=1"},
    )

    with store.connection() as conn:
        raw = str(conn.execute("SELECT payload_json FROM audit_events").fetchone()[0])

    assert canary not in raw
