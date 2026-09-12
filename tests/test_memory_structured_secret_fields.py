from __future__ import annotations

import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService

_AUTH_OPAQUE = "opaque-authorization-secret-canary"
_TOKEN_OPAQUE = "opaque-token-secret-canary"
_NESTED_OPAQUE = "opaque-nested-secret-canary"
_DYNAMIC_BEARER = "dynamic-key-secret-canary"
_BENIGN_DYNAMIC_VALUE = "upstream"
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
            dynamic_authorization_key: _BENIGN_DYNAMIC_VALUE,
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
    ):
        assert secret not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable["authorization"] == {
        "credential": "[REDACTED]",
        "metadata": {"scheme": "[REDACTED]", "attempt": "[REDACTED]"},
        "Authorization: [REDACTED]": _BENIGN_DYNAMIC_VALUE,
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
