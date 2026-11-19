from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nika_core.artifacts import ArtifactRegistry, ArtifactRegistryError
from nika_core.data.sqlite import SQLiteStore


_ERROR_TYPES = (ArtifactRegistryError, ValidationError)
_CANARY = "NIKA_ARTIFACT_SECRET_CANARY_522"


def _assert_canary_not_persisted(store: SQLiteStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT record_json FROM artifact_registry_records"
        ).fetchall()
    assert all(_CANARY not in str(row["record_json"]) for row in rows)


@pytest.mark.parametrize(
    "reference_template",
    (
        "https://example.test/object?api_key={canary}",
        "https://example.test/object?api%5Fkey={canary}",
        "https://example.test/object?client_secret={canary}",
        "https://example.test/object?x-api-key={canary}",
    ),
)
def test_common_credential_locators_fail_before_persistence(
    tmp_path: Path,
    reference_template: str,
) -> None:
    """Common API-key/secret URL forms must never become durable artifact metadata."""
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store)
    reference = reference_template.format(canary=_CANARY)

    with pytest.raises(_ERROR_TYPES, match="credential|secret"):
        registry.register_reference(
            workspace_id="workspace-secret-oracle",
            idempotency_key=reference_template,
            reference=reference,
            sha256="a" * 64,
            size_bytes=1,
            kind="evidence",
        )

    _assert_canary_not_persisted(store)


@pytest.mark.parametrize(
    "secret_key",
    (
        "client_secret",
        "x-api-key",
        "api-token",
    ),
)
def test_common_secret_metadata_keys_fail_before_persistence(
    tmp_path: Path,
    secret_key: str,
) -> None:
    """Credential-shaped metadata keys must fail before their values reach SQLite."""
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store)

    with pytest.raises(_ERROR_TYPES, match="credential|secret"):
        registry.register_reference(
            workspace_id="workspace-secret-metadata-oracle",
            idempotency_key=secret_key,
            reference="blob:safe-reference",
            sha256="b" * 64,
            size_bytes=1,
            kind="evidence",
            metadata={secret_key: _CANARY},
        )

    _assert_canary_not_persisted(store)
