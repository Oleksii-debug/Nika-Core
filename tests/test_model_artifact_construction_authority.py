from __future__ import annotations

import hashlib

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactRegistry,
    ModelArtifactResources,
    ModelIntegrityBasis,
)


def _descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="provider",
        model_id="model",
        model_version="v1",
        source_reference="https://models.example.test/model",
        license_reference="https://licenses.example.test/model",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=hashlib.sha256(b"weights").hexdigest(),
        size_bytes=len(b"weights"),
        capabilities=("text",),
        resources=ModelArtifactResources(cpu_architectures=("x86_64",)),
    )


def _forged_resources(
    canonical: ModelArtifactResources,
    **overrides: object,
) -> ModelArtifactResources:
    forged = object.__new__(ModelArtifactResources)
    values: dict[str, object] = {
        "min_system_memory_bytes": canonical.min_system_memory_bytes,
        "min_available_memory_bytes": canonical.min_available_memory_bytes,
        "min_vram_bytes": canonical.min_vram_bytes,
        "recommended_memory_bytes": canonical.recommended_memory_bytes,
        "cpu_architectures": canonical.cpu_architectures,
    }
    values.update(overrides)
    for name, value in values.items():
        object.__setattr__(forged, name, value)
    return forged


def _forged_descriptor(
    canonical: ModelArtifactDescriptor,
    **overrides: object,
) -> ModelArtifactDescriptor:
    forged = object.__new__(ModelArtifactDescriptor)
    values: dict[str, object] = {
        "kind": canonical.kind,
        "provider_id": canonical.provider_id,
        "model_id": canonical.model_id,
        "source_reference": canonical.source_reference,
        "license_reference": canonical.license_reference,
        "integrity_basis": canonical.integrity_basis,
        "model_version": canonical.model_version,
        "sha256": canonical.sha256,
        "size_bytes": canonical.size_bytes,
        "capabilities": canonical.capabilities,
        "resources": canonical.resources,
        "schema_version": canonical.schema_version,
    }
    values.update(overrides)
    for name, value in values.items():
        object.__setattr__(forged, name, value)
    return forged


def _authority_counts(store: SQLiteStore) -> tuple[int, int]:
    with store.connection() as conn:
        artifacts = int(conn.execute("SELECT COUNT(*) FROM model_artifacts").fetchone()[0])
        audits = int(
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
                ("model.artifact.registered",),
            ).fetchone()[0]
        )
    return artifacts, audits


def test_registry_revalidates_forged_descriptor_before_persistence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    registry = ModelArtifactRegistry(store)
    forged = _forged_descriptor(
        _descriptor(),
        source_reference="token=MODEL_CANARY",
    )

    with pytest.raises(ValueError, match="credential material"):
        registry.register(forged)

    assert _authority_counts(store) == (0, 0)


def test_registry_revalidates_forged_nested_resources_before_persistence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    registry = ModelArtifactRegistry(store)
    canonical = _descriptor()
    resources = _forged_resources(
        canonical.resources,
        min_system_memory_bytes=-1,
    )
    forged = _forged_descriptor(canonical, resources=resources)

    with pytest.raises(ValueError, match="range"):
        registry.register(forged)

    assert _authority_counts(store) == (0, 0)
