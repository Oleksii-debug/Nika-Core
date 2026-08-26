from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.artifacts import (
    ArtifactRegistry,
    ArtifactRegistryError,
    ArtifactVerificationState,
    initialize_artifact_registry_schema,
)
from nika_core.data.sqlite import SQLiteStore


def _registry(store: SQLiteStore, *, local_root: Path | None = None) -> ArtifactRegistry:
    roots: tuple[Path, ...] = () if local_root is None else (local_root,)
    return ArtifactRegistry.from_store(store, local_file_roots=roots)


def _register_reference(registry: ArtifactRegistry):
    return registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="artifact-a",
        reference="blob:workspace-a/artifact-a",
        sha256="a" * 64,
        size_bytes=7,
        kind="evidence",
        producer_id="agent-a",
    )


def test_record_json_cannot_rebind_identity_behind_primary_key(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = _registry(store)
    record = _register_reference(registry)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT record_json FROM artifact_registry_records WHERE artifact_id = ?",
            (record.artifact_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["record_json"])
        payload["artifact_id"] = "f" * 64
        conn.execute(
            "UPDATE artifact_registry_records SET record_json = ? WHERE artifact_id = ?",
            (json.dumps(payload, separators=(",", ":")), record.artifact_id),
        )

    with pytest.raises(ArtifactRegistryError):
        registry.get(record.artifact_id)


def test_query_columns_cannot_launder_record_into_another_workspace(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = _registry(store)
    _register_reference(registry)

    with store.connection() as conn:
        conn.execute(
            "UPDATE artifact_registry_records SET workspace_id = ?",
            ("workspace-forged",),
        )

    try:
        records = registry.list(workspace_id="workspace-forged")
    except ArtifactRegistryError:
        return
    assert records == ()


def test_migration_history_rejects_non_integer_storage_type(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE artifact_registry_schema_migrations SET version = ?",
            (1.5,),
        )

    with pytest.raises(RuntimeError):
        initialize_artifact_registry_schema(store)


def test_current_schema_marker_cannot_hide_malformed_owned_tables(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP TABLE artifact_registry_verifications")
        conn.execute("DROP TABLE artifact_registry_records")
        conn.execute(
            "CREATE TABLE artifact_registry_records(artifact_id TEXT PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError):
        initialize_artifact_registry_schema(store)


def test_verification_json_cannot_rebind_evidence_to_another_artifact(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = _registry(store)
    record = _register_reference(registry)
    verification = registry.verify(record.artifact_id)
    assert verification.state == ArtifactVerificationState.UNAVAILABLE

    with store.connection() as conn:
        row = conn.execute(
            "SELECT verification_json FROM artifact_registry_verifications "
            "WHERE verification_id = ?",
            (verification.verification_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["verification_json"])
        payload["artifact_id"] = "f" * 64
        conn.execute(
            "UPDATE artifact_registry_verifications SET verification_json = ? "
            "WHERE verification_id = ?",
            (
                json.dumps(payload, separators=(",", ":")),
                verification.verification_id,
            ),
        )

    with pytest.raises(ArtifactRegistryError):
        registry.verification_history(record.artifact_id)


def test_verify_rejects_symlink_substitution_outside_allowed_root(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    source = allowed / "artifact.bin"
    source.write_bytes(b"trusted-bytes")
    external = outside / "artifact.bin"
    external.write_bytes(b"trusted-bytes")

    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = _registry(store, local_root=allowed)
    record = registry.register_file(
        workspace_id="workspace-a",
        idempotency_key="local-a",
        path=source,
        kind="evidence",
    )
    source.unlink()
    try:
        source.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable in this environment")

    with pytest.raises(ArtifactRegistryError):
        registry.verify(record.artifact_id)
