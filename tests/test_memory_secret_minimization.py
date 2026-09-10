from __future__ import annotations

import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService

_API_TOKEN = "sk-nika-memory-secret-123"
_AUTH_SECRET = "auth-header-secret-456"
_PASSWORD = "hunter2-memory-secret-789"
_SIGNED_SECRET = "signed-url-secret-abc"
_LOCAL_PATH = "/home/alice/.config/nika/private-result.json"


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _raw_memory_value(store: SQLiteStore) -> str:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope=? AND owner_id=? "
            "AND namespace=? AND memory_key=?",
            ("workspace", "research", "inference", "candidate"),
        ).fetchone()
    assert row is not None
    return str(row["value_json"])


def test_memory_persistence_minimizes_model_and_tool_secrets_across_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    value = {
        "model_output": {
            "api_key": _API_TOKEN,
            "explanation": f"Authorization: Bearer {_AUTH_SECRET}",
            "notes": f"password={_PASSWORD}",
        },
        "tool_result": {
            "download_url": (
                "https://example.test/result?signature="
                f"{_SIGNED_SECRET}&expires=1700000000&page=1"
            ),
            "local_path": _LOCAL_PATH,
        },
        "benign": {
            "status": "ok",
            "token_count": 17,
            "public_url": "https://example.test/docs?page=2",
            "relative_path": "docs/guide.md",
            "message": "password policy enabled",
        },
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="candidate",
        value=value,
    )

    raw_before_restart = _raw_memory_value(first_store)
    for secret in (_API_TOKEN, _AUTH_SECRET, _PASSWORD, _SIGNED_SECRET, _LOCAL_PATH):
        assert secret not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable["model_output"] == {
        "api_key": "[REDACTED]",
        "explanation": "Authorization: [REDACTED]",
        "notes": "password=[REDACTED]",
    }
    assert durable["tool_result"] == {
        "download_url": (
            "https://example.test/result?signature=[REDACTED]"
            "&expires=[REDACTED]&page=1"
        ),
        "local_path": "[LOCAL_PATH]",
    }
    assert durable["benign"] == value["benign"]

    restarted_store = _store(db_path)
    restarted = MemoryService(restarted_store)
    record = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="candidate",
    )
    assert record is not None
    assert record.value == durable

    raw_after_restart = _raw_memory_value(restarted_store)
    assert raw_after_restart == raw_before_restart
    for secret in (_API_TOKEN, _AUTH_SECRET, _PASSWORD, _SIGNED_SECRET, _LOCAL_PATH):
        assert secret not in raw_after_restart
