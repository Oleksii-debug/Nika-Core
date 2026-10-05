from __future__ import annotations

import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactRegistry,
    ModelArtifactResources,
    ModelIntegrityBasis,
)
from nika_core.v01_model_settings import V01ModelSettings


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "model provenance.db")
    store.initialize()
    return store


def _configure_local(settings: V01ModelSettings, *, model: str = "qwen3:8b") -> None:
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": model,
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
            "revision": 0,
        }
    )
    assert result.status == "completed"


def _descriptor(*, model_id: str = "qwen3:8b") -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id=model_id,
        model_version="8b-reviewed",
        source_reference="https://ollama.com/library/qwen3",
        license_reference="https://qwenlm.github.io/license",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256="a" * 64,
        size_bytes=1 << 60,
        capabilities=("text", "text.chat"),
        resources=ModelArtifactResources(
            min_system_memory_bytes=8 * 1024**3,
            min_available_memory_bytes=2 * 1024**3,
            min_vram_bytes=4 * 1024**3,
            recommended_memory_bytes=16 * 1024**3,
            cpu_architectures=("amd64", "x86_64"),
        ),
    )


def test_unregistered_selected_model_is_not_given_synthetic_provenance(tmp_path: Path) -> None:
    settings = V01ModelSettings(_store(tmp_path))
    _configure_local(settings)

    snapshot = settings.snapshot()

    assert snapshot["status"] == "ready"
    assert snapshot["provider_id"] == "ollama"
    assert snapshot["model"] == "qwen3:8b"
    assert snapshot["artifact"] == {"status": "unregistered"}


def test_deterministic_route_marks_artifact_provenance_not_applicable(tmp_path: Path) -> None:
    settings = V01ModelSettings(_store(tmp_path))
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "deterministic",
            "provider_id": None,
            "model": None,
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
            "revision": 0,
        }
    )
    assert result.status == "completed"

    assert settings.snapshot()["artifact"] == {"status": "not_applicable"}


def test_registered_selected_model_projects_only_validated_public_provenance(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure_local(settings)
    descriptor = _descriptor()
    ModelArtifactRegistry(store).register(descriptor)

    snapshot = settings.snapshot()
    artifact = snapshot["artifact"]

    assert artifact == {
        "status": "registered",
        "kind": "external_local",
        "model_version": "8b-reviewed",
        "source_reference": "https://ollama.com/library/qwen3",
        "license_reference": "https://qwenlm.github.io/license",
        "integrity_basis": "sha256",
        "sha256": "a" * 64,
        "size_bytes": str(1 << 60),
        "descriptor_digest": descriptor.descriptor_digest,
        "capabilities": ["text", "text.chat"],
        "resources": {
            "min_system_memory_bytes": str(8 * 1024**3),
            "min_available_memory_bytes": str(2 * 1024**3),
            "min_vram_bytes": str(4 * 1024**3),
            "recommended_memory_bytes": str(16 * 1024**3),
            "cpu_architectures": ["amd64", "x86_64"],
        },
    }
    rendered = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    assert "credential_ref" not in rendered
    assert "env:" not in rendered


def test_registered_provenance_survives_fresh_settings_instance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure_local(settings)
    ModelArtifactRegistry(store).register(_descriptor())

    restarted = V01ModelSettings(SQLiteStore(store.path))

    assert restarted.snapshot()["artifact"] == settings.snapshot()["artifact"]


def test_registry_corruption_marks_only_provenance_invalid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure_local(settings)
    ModelArtifactRegistry(store).register(_descriptor())
    with store.connection() as conn:
        conn.execute(
            "UPDATE model_artifacts SET descriptor_digest = ? "
            "WHERE provider_id = ? AND model_id = ?",
            ("0" * 64, "ollama", "qwen3:8b"),
        )

    snapshot = settings.snapshot()

    assert snapshot["status"] == "ready"
    assert snapshot["provider_id"] == "ollama"
    assert snapshot["model"] == "qwen3:8b"
    assert snapshot["artifact"] == {"status": "invalid"}


def test_provenance_lookup_is_bound_to_exact_selected_model_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure_local(settings, model="qwen3:8b")
    ModelArtifactRegistry(store).register(_descriptor(model_id="qwen3:14b"))

    snapshot = settings.snapshot()

    assert snapshot["artifact"] == {"status": "unregistered"}
