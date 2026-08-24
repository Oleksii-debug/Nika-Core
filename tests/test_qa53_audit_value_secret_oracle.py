from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog


@pytest.mark.parametrize(
    ("field", "value", "canary"),
    [
        (
            "error",
            "request failed with Authorization: Bearer QA53_CANARY_AUTH_VALUE_71C4",
            "QA53_CANARY_AUTH_VALUE_71C4",
        ),
        (
            "detail",
            "provider rejected api_key=QA53_CANARY_PROVIDER_KEY_2A6E",
            "QA53_CANARY_PROVIDER_KEY_2A6E",
        ),
        (
            "url",
            "https://qa-user:QA53_CANARY_URL_PASSWORD_84D1@audit.invalid/path"
            "?access_token=QA53_CANARY_QUERY_TOKEN_63B9",
            "QA53_CANARY_URL_PASSWORD_84D1",
        ),
        (
            "stderr",
            "fatal: repository 'https://oauth2:QA53_CANARY_GIT_TOKEN_5F20@github.invalid/"
            "org/repo.git/' not found",
            "QA53_CANARY_GIT_TOKEN_5F20",
        ),
        (
            "message",
            "Cookie: sessionid=QA53_CANARY_COOKIE_9BE3",
            "QA53_CANARY_COOKIE_9BE3",
        ),
    ],
)
def test_qa53_oracle_secret_content_in_benign_audit_fields_never_reaches_sqlite(
    tmp_path: Path,
    field: str,
    value: str,
    canary: str,
) -> None:
    store = SQLiteStore(tmp_path / "qa53-audit.db")
    store.initialize()
    audit = AuditLog(store)
    audit.append(
        event_type="qa53-secret-leak-oracle",
        entity_type="security",
        entity_id="qa53",
        payload={field: value},
    )
    with store.connection() as conn:
        raw = str(conn.execute("SELECT payload_json FROM audit_events").fetchone()[0])
    assert canary not in raw


def test_qa53_oracle_query_token_in_url_value_never_reaches_sqlite(tmp_path: Path) -> None:
    canary = "QA53_CANARY_QUERY_TOKEN_63B9"
    store = SQLiteStore(tmp_path / "qa53-query.db")
    store.initialize()
    audit = AuditLog(store)
    audit.append(
        event_type="qa53-url-leak-oracle",
        entity_type="security",
        entity_id="qa53",
        payload={"url": "https://audit.invalid/path?access_token=" + canary + "&page=1"},
    )
    with store.connection() as conn:
        raw = str(conn.execute("SELECT payload_json FROM audit_events").fetchone()[0])
    assert canary not in raw
