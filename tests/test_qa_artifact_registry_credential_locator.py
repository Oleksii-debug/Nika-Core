from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nika_core.artifacts import ArtifactRegistry, ArtifactRegistryError
from nika_core.data.sqlite import SQLiteStore


@pytest.mark.parametrize(
    "reference_template",
    (
        "https://example.test/object?api_key={canary}",
        "https://example.test/object?client_secret={canary}",
        "https://example.test/object?x-api-key={canary}",
    ),
)
def test_common_credential_locators_fail_before_persistence(
    tmp_path: Path,
    reference_template: str,
) -> None:
    """Common API-key/secret locators must never become durable artifact metadata."""
    canary = "NIKA_ARTIFACT_SECRET_CANARY_522"
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store)
    reference = reference_template.format(canary=canary)

    with pytest.raises((ArtifactRegistryError, ValidationError), match="credential"):
        registry.register_reference(
            workspace_id="workspace-secret-oracle",
            idempotency_key=reference_template,
            reference=reference,
            sha256="a" * 64,
            size_bytes=1,
            kind="evidence",
        )

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT record_json FROM artifact_registry_records"
        ).fetchall()
    assert all(canary not in str(row["record_json"]) for row in rows)
