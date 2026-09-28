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
_URL_USERINFO_USER = "private-memory-user"
_URL_USERINFO_SECRET = "userinfo-password-secret"
_URL_USERINFO_KEY_SECRET = "userinfo-key-secret"
_AWS_CREDENTIAL = "AKIAEXAMPLE/20260929/eu-central-1/s3/aws4_request"
_AWS_SIGNATURE = "aws-signature-secret"
_AWS_SECURITY_TOKEN = "aws-security-token-secret"
_GOOG_CREDENTIAL = "service@example.test/20260929/auto/storage/goog4_request"
_GOOG_SIGNATURE = "goog-signature-secret"
_FRAGMENT_ACCESS_TOKEN = "fragment-access-token-secret"


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
    userinfo_url = (
        f"https://{_URL_USERINFO_USER}:{_URL_USERINFO_SECRET}"
        "@example.test/private?view=1"
    )
    userinfo_key = (
        f"https://cache-user:{_URL_USERINFO_KEY_SECRET}"
        "@example.test/cache"
    )
    aws_signed_url = (
        "https://storage.example.test/object?"
        f"X-Amz-Credential={_AWS_CREDENTIAL}&X-Amz-Signature={_AWS_SIGNATURE}"
        f"&X-Amz-Security-Token={_AWS_SECURITY_TOKEN}&partNumber=1"
    )
    goog_signed_url = (
        "https://storage.example.test/object?"
        f"X-Goog-Credential={_GOOG_CREDENTIAL}&X-Goog-Signature={_GOOG_SIGNATURE}"
        "&generation=1"
    )
    fragment_url = (
        "https://auth.example.test/callback#access_token="
        f"{_FRAGMENT_ACCESS_TOKEN}&state=stable"
    )
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
            "credential_url": userinfo_url,
            "aws_signed_url": aws_signed_url,
            "goog_signed_url": goog_signed_url,
            "fragment_url": fragment_url,
        },
        "dynamic_keys": {
            "posix": {_POSIX_KEY: "posix"},
            "windows_backslash": {_WINDOWS_BACKSLASH_KEY: "windows-backslash"},
            "windows_slash": {_WINDOWS_SLASH_KEY: "windows-slash"},
            "signed_url": {signed_key: "cached"},
            "authorization": {authorization_key: "upstream"},
            "userinfo_url": {userinfo_key: "cached"},
        },
        "benign": {
            "status": "ok",
            "token_count": 17,
            "public_url": "https://example.test/docs?page=2",
            "relative_path": "docs/guide.md",
            "message": "password policy enabled",
            "email": "alice@example.test",
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
        _URL_USERINFO_USER,
        _URL_USERINFO_SECRET,
        _URL_USERINFO_KEY_SECRET,
        _AWS_CREDENTIAL,
        _AWS_SIGNATURE,
        _AWS_SECURITY_TOKEN,
        _GOOG_CREDENTIAL,
        _GOOG_SIGNATURE,
        _FRAGMENT_ACCESS_TOKEN,
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
        "credential_url": "https://[REDACTED]@example.test/private?view=1",
        "aws_signed_url": (
            "https://storage.example.test/object?"
            "X-Amz-Credential=[REDACTED]&X-Amz-Signature=[REDACTED]"
            "&X-Amz-Security-Token=[REDACTED]&partNumber=1"
        ),
        "goog_signed_url": (
            "https://storage.example.test/object?"
            "X-Goog-Credential=[REDACTED]&X-Goog-Signature=[REDACTED]"
            "&generation=1"
        ),
        "fragment_url": (
            "https://auth.example.test/callback#access_token=[REDACTED]&state=stable"
        ),
    }
    assert durable["dynamic_keys"] == {
        "posix": {"[LOCAL_PATH]": "posix"},
        "windows_backslash": {"[LOCAL_PATH]": "windows-backslash"},
        "windows_slash": {"[LOCAL_PATH]": "windows-slash"},
        "signed_url": {
            "https://example.test/result?signature=[REDACTED]"
            "&expires=[REDACTED]&page=1": "cached"
        },
        "authorization": {"Authorization: [REDACTED]": "[REDACTED]"},
        "userinfo_url": {"https://[REDACTED]@example.test/cache": "cached"},
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
        "extensionless_backslash": (
            r"Opened C:\Users\Alice Smith\Private Data successfully"
        ),
        "extensionless_slash": "Opened C:/Users/Alice Smith/Secret Notes now",
        "extensionless_mixed": r"Opened C:\Users/Alice Smith\Private Data now",
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="embedded-windows-paths",
        value=messages,
    )

    raw_before_restart = _raw_memory_value(first_store, key="embedded-windows-paths")
    for fragment in ("Alice Smith", "Private Data", "Secret Notes", "result.txt"):
        assert fragment not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable == {
        "backslash": "Opened [LOCAL_PATH] successfully",
        "slash": "Opened [LOCAL_PATH] successfully",
        "mixed": "Opened [LOCAL_PATH] successfully",
        "spaced_file": "Opened [LOCAL_PATH] successfully",
        # Extensionless components with spaces are syntactically ambiguous in
        # prose, so the minimizer deliberately fails closed through end-of-text.
        "extensionless_backslash": "Opened [LOCAL_PATH]",
        "extensionless_slash": "Opened [LOCAL_PATH]",
        "extensionless_mixed": "Opened [LOCAL_PATH]",
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


def test_memory_persistence_fully_redacts_provider_and_fragment_credentials(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    aws_secret = "aws-signature-secret"
    fragment_secret = "fragment-access-token-secret"
    userinfo_secret = "userinfo-password-secret"
    value = {
        "aws": (
            "Observed https://storage.example.test/object?"
            f"X-Amz-Signature={aws_secret} next=stable"
        ),
        "fragment": (
            "Observed https://auth.example.test/callback#access_token="
            f"{fragment_secret} state=stable"
        ),
        "userinfo": f"Observed https://alice:{userinfo_secret}@example.test/private",
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="credential-boundaries",
        value=value,
    )

    raw_before_restart = _raw_memory_value(first_store, key="credential-boundaries")
    for secret in (aws_secret, fragment_secret, userinfo_secret):
        assert secret not in raw_before_restart
    for leaked_suffix in (
        "s-signature-secret",
        "ss-token-secret",
        "sword-secret",
    ):
        assert leaked_suffix not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable == {
        "aws": (
            "Observed https://storage.example.test/object?"
            "X-Amz-Signature=[REDACTED] next=stable"
        ),
        "fragment": (
            "Observed https://auth.example.test/callback#access_token="
            "[REDACTED] state=stable"
        ),
        "userinfo": "Observed https://[REDACTED]@example.test/private",
    }

    restarted_store = _store(db_path)
    restarted = MemoryService(restarted_store)
    record = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="credential-boundaries",
    )
    assert record is not None
    assert record.value == durable
    raw_after_restart = _raw_memory_value(
        restarted_store,
        key="credential-boundaries",
    )
    assert raw_after_restart == raw_before_restart
    for secret in (aws_secret, fragment_secret, userinfo_secret):
        assert secret not in raw_after_restart


def test_memory_persistence_redacts_oidc_id_token_text_across_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    oidc_secret = "eyJ-oidc-id-token-secret"
    value = {
        "assignment": f"id_token={oidc_secret}",
        "hyphen_assignment": f"id-token={oidc_secret}",
        "compact_assignment": f"idtoken={oidc_secret}",
        "query": (
            "https://auth.example.test/callback?"
            f"id_token={oidc_secret}&state=stable"
        ),
        "fragment": (
            "https://auth.example.test/callback#"
            f"id_token={oidc_secret}&state=stable"
        ),
        "benign": "id_token_count=3",
    }

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="oidc-id-token-boundaries",
        value=value,
    )

    raw_before_restart = _raw_memory_value(
        first_store,
        key="oidc-id-token-boundaries",
    )
    assert oidc_secret not in raw_before_restart

    durable = json.loads(raw_before_restart)
    assert durable == {
        "assignment": "id_token=[REDACTED]",
        "hyphen_assignment": "id-token=[REDACTED]",
        "compact_assignment": "idtoken=[REDACTED]",
        "query": (
            "https://auth.example.test/callback?"
            "id_token=[REDACTED]&state=stable"
        ),
        "fragment": (
            "https://auth.example.test/callback#"
            "id_token=[REDACTED]&state=stable"
        ),
        "benign": "id_token_count=3",
    }

    restarted_store = _store(db_path)
    restarted = MemoryService(restarted_store)
    record = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="inference",
        key="oidc-id-token-boundaries",
    )
    assert record is not None
    assert record.value == durable

    raw_after_restart = _raw_memory_value(
        restarted_store,
        key="oidc-id-token-boundaries",
    )
    assert raw_after_restart == raw_before_restart
    assert oidc_secret not in raw_after_restart


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


def test_memory_persistence_fails_closed_on_url_userinfo_key_collision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    store = _store(db_path)
    memory = MemoryService(store)
    first_secret = "userinfo-collision-first"
    second_secret = "userinfo-collision-second"

    with pytest.raises(ValueError) as exc_info:
        memory.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="research",
            namespace="inference",
            key="userinfo-collision",
            value={
                f"https://alice:{first_secret}@example.test/cache": "first",
                f"https://bob:{second_secret}@example.test/cache": "second",
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
            ("workspace", "research", "inference", "userinfo-collision"),
        ).fetchone()
    assert row is None
