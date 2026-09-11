from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService

_API_TOKEN = "sk-nika-memory-secret-123"
_AUTH_SECRET = "auth-header-secret-456"
_PASSWORD = "hunter2-memory-secret-789"
_SIGNED_SECRET = "signed-url-secret-abc"
_LOCAL_PATH = "/home/alice/.config/nika/private-result.json"
_MAPPING_SIGNATURE = "mapping-key-signature-secret"
_MAPPING_BEARER = "mapping-key-bearer-secret"
_POSIX_KEY = "/home/alice/private-result.json"
_WINDOWS_BACKSLASH_KEY = r"C:\Users\Alice Smith\Private Data\result.txt"
_WINDOWS_SLASH_KEY = "C:/Users/Alice Smith/Private Data/result.txt"


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _raw_memory_value(store: SQLiteStore, *, key: str = "candidate") -> str:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope=? AND owner_id=? "
            "AND namespace=? AND memory_key=?",
            ("workspace", "research", "inference", key),
        ).fetchone()
    assert row is not None
    return str(row["value_json"])


def test_memory_persistence_minimizes_model_and_tool_secrets_across_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    signed_key = (
        "https://example.test/result?"
        f"signature={_MAPPING_SIGNATURE}&expires=1700000000&page=1"
    )
    authorization_key = f"Authorization: Bearer {_MAPPING_BEARER}"
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
        "dynamic_keys": {
            "posix": {_POSIX_KEY: "posix"},
            "windows_backslash": {_WINDOWS_BACKSLASH_KEY: "windows-backslash"},
            "windows_slash": {_WINDOWS_SLASH_KEY: "windows-slash"},
            "signed_url": {signed_key: "cached"},
            "authorization": {authorization_key: "upstream"},
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
    sensitive_fragments = (
        _API_TOKEN,
        _AUTH_SECRET,
        _PASSWORD,
        _SIGNED_SECRET,
        _LOCAL_PATH,
        _MAPPING_SIGNATURE,
        _MAPPING_BEARER,
        _POSIX_KEY,
        _WINDOWS_BACKSLASH_KEY,
        _WINDOWS_SLASH_KEY,
    )
    for secret in sensitive_fragments:
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
    assert durable["dynamic_keys"] == {
        "posix": {"[LOCAL_PATH]": "posix"},
        "windows_backslash": {"[LOCAL_PATH]": "windows-backslash"},
        "windows_slash": {"[LOCAL_PATH]": "windows-slash"},
        "signed_url": {
            "https://example.test/result?signature=[REDACTED]"
            "&expires=[REDACTED]&page=1": "[REDACTED]"
        },
        "authorization": "[REDACTED]",
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
    for secret in sensitive_fragments:
        assert secret not in raw_after_restart


def test_memory_persistence_redacts_embedded_windows_paths_across_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    messages = {
        "backslash": r"Opened C:\Users\Alice Smith\Private Data\result.txt successfully",
        "slash": "Opened C:/Users/Alice Smith/Private Data/result.txt successfully",
        "mixed": r"Opened C:\Users/Alice Smith\Private Data/result.txt successfully",
        "spaced_file": (
            r"Opened C:\Users\Alice Smith\Private Data\final result.txt successfully"
        ),
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="embedded-windows-paths",
        value=messages,
    )

    raw_before_restart = _raw_memory_value(first_store, key="embedded-windows-paths")
    for fragment in ("Alice Smith", "Private Data", "result.txt"):
        assert fragment not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable == {
        name: "Opened [LOCAL_PATH] successfully"
        for name in messages
    }

    restarted_store = _store(db_path)
    restarted = MemoryService(restarted_store)
    record = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="embedded-windows-paths",
    )
    assert record is not None
    assert record.value == durable
    assert _raw_memory_value(
        restarted_store,
        key="embedded-windows-paths",
    ) == raw_before_restart


def test_memory_persistence_fails_closed_on_redacted_mapping_key_collision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    store = _store(db_path)
    memory = MemoryService(store)
    first_secret = "collision-first-secret"
    second_secret = "collision-second-secret"

    with pytest.raises(ValueError) as exc_info:
        memory.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="research",
            namespace="inference",
            key="collision",
            value={
                f"Authorization: Bearer {first_secret}": "first",
                f"Authorization: Bearer {second_secret}": "second",
            },
        )

    assert str(exc_info.value) == "memory persistence key collision after minimization"
    assert first_secret not in str(exc_info.value)
    assert second_secret not in str(exc_info.value)

    restarted_store = _store(db_path)
    with restarted_store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope=? AND owner_id=? "
            "AND namespace=? AND memory_key=?",
            ("workspace", "research", "inference", "collision"),
        ).fetchone()
    assert row is None
