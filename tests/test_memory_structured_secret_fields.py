from __future__ import annotations

import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService

_AUTH_OPAQUE = "opaque-authorization-secret-canary"
_TOKEN_OPAQUE = "opaque-token-secret-canary"
_NESTED_OPAQUE = "opaque-nested-secret-canary"
_DYNAMIC_BEARER = "dynamic-key-secret-canary"
_DYNAMIC_ASSOCIATED_OPAQUE = "opaque-dynamic-associated-secret-canary"
_BENIGN_OPAQUE_VALUE = "opaque-benign-value"


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _raw_memory_value(store: SQLiteStore) -> str:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope=? AND owner_id=? "
            "AND namespace=? AND memory_key=?",
            ("workspace", "research", "inference", "structured-secrets"),
        ).fetchone()
    assert row is not None
    return str(row["value_json"])


def test_structured_canonical_secret_fields_fail_closed_across_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    dynamic_authorization_key = f"Authorization: Bearer {_DYNAMIC_BEARER}"
    value = {
        "authorization": {
            "credential": _AUTH_OPAQUE,
            "metadata": {"scheme": "Bearer", "attempt": 3},
            dynamic_authorization_key: _DYNAMIC_ASSOCIATED_OPAQUE,
        },
        "token": [
            _TOKEN_OPAQUE,
            {"credential": _NESTED_OPAQUE},
        ],
        "benign": {"credential": _BENIGN_OPAQUE_VALUE},
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="structured-secrets",
        value=value,
    )

    raw_before_restart = _raw_memory_value(first_store)
    for secret in (
        _AUTH_OPAQUE,
        _TOKEN_OPAQUE,
        _NESTED_OPAQUE,
        _DYNAMIC_BEARER,
        _DYNAMIC_ASSOCIATED_OPAQUE,
    ):
        assert secret not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable["authorization"] == {
        "credential": "[REDACTED]",
        "metadata": {"scheme": "[REDACTED]", "attempt": "[REDACTED]"},
        "Authorization: [REDACTED]": "[REDACTED]",
    }
    assert durable["token"] == [
        "[REDACTED]",
        {"credential": "[REDACTED]"},
    ]
    assert durable["benign"] == {"credential": _BENIGN_OPAQUE_VALUE}

    restarted_store = _store(db_path)
    restarted = MemoryService(restarted_store)
    record = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="structured-secrets",
    )
    assert record is not None
    assert record.value == durable
    assert _raw_memory_value(restarted_store) == raw_before_restart


def test_structured_provider_credential_fields_fail_closed_across_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    secrets = {
        "aws_credential": "AKIAEXAMPLE/20261005/eu-central-1/s3/aws4_request",
        "aws_signature": "aws-structured-signature-secret",
        "aws_token": "aws-structured-security-token-secret",
        "aws_sse_key": "aws-customer-encryption-key-secret",
        "goog_credential": "service@example.test/20261005/auto/storage/goog4_request",
        "goog_signature": "goog-structured-signature-secret",
        "goog_key": "goog-customer-encryption-key-secret",
        "azure_key": "azure-customer-encryption-key-secret",
        "nested_identity": "nested-provider-identity-secret",
        "nested_scope": "nested-provider-scope-secret",
    }
    value = {
        "headers": {
            "X-Amz-Credential": secrets["aws_credential"],
            "X-Amz-Signature": secrets["aws_signature"],
            "X-Amz-Security-Token": secrets["aws_token"],
            "X-Amz-Server-Side-Encryption-Customer-Key": secrets["aws_sse_key"],
            "X-Goog-Credential": secrets["goog_credential"],
            "X-Goog-Signature": secrets["goog_signature"],
            "X-Goog-Encryption-Key": secrets["goog_key"],
            "X-Ms-Encryption-Key": secrets["azure_key"],
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
        },
        "structured": {
            "X-Goog-Credential": {
                "identity": secrets["nested_identity"],
                "scope": secrets["nested_scope"],
            }
        },
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="provider-credentials",
        value=value,
    )

    with first_store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope=? AND owner_id=? "
            "AND namespace=? AND memory_key=?",
            ("workspace", "research", "inference", "provider-credentials"),
        ).fetchone()
    assert row is not None
    raw_before_restart = str(row["value_json"])
    for secret in secrets.values():
        assert secret not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable["headers"] == {
        "X-Amz-Credential": "[REDACTED]",
        "X-Amz-Signature": "[REDACTED]",
        "X-Amz-Security-Token": "[REDACTED]",
        "X-Amz-Server-Side-Encryption-Customer-Key": "[REDACTED]",
        "X-Goog-Credential": "[REDACTED]",
        "X-Goog-Signature": "[REDACTED]",
        "X-Goog-Encryption-Key": "[REDACTED]",
        "X-Ms-Encryption-Key": "[REDACTED]",
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
    }
    assert durable["structured"] == {
        "X-Goog-Credential": {
            "identity": "[REDACTED]",
            "scope": "[REDACTED]",
        }
    }

    restarted = MemoryService(_store(db_path))
    record = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="provider-credentials",
    )
    assert record is not None
    assert record.value == durable
