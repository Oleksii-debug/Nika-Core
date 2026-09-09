from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.model_artifacts import (
    ModelArtifactConflictError,
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactRegistry,
    ModelArtifactRegistryError,
    ModelArtifactResources,
    ModelIntegrityBasis,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Профіль Nika" / "Моделі" / "ніка.db")
    store.initialize()
    return store


def _descriptor(
    *,
    model_id: str = "foundry-public-model:7",
    license_reference: str = "https://licenses.example.test/models/foundry-7",
) -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="foundry-local",
        model_id=model_id,
        model_version="7",
        source_reference="https://models.example.test/catalog/foundry-public-model",
        license_reference=license_reference,
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        capabilities=("text.chat", "text"),
        resources=ModelArtifactResources(
            min_system_memory_bytes=8 * 1024**3,
            min_available_memory_bytes=2 * 1024**3,
            recommended_memory_bytes=16 * 1024**3,
            cpu_architectures=("x86_64", "amd64", "amd64"),
        ),
    )


def test_descriptor_normalizes_set_like_metadata_and_has_stable_digest() -> None:
    first = _descriptor(model_id="модель-українська:7")
    second = replace(
        first,
        capabilities=("text", "text.chat", "text"),
        resources=ModelArtifactResources(
            min_system_memory_bytes=8 * 1024**3,
            min_available_memory_bytes=2 * 1024**3,
            recommended_memory_bytes=16 * 1024**3,
            cpu_architectures=("amd64", "x86_64"),
        ),
    )

    assert first.capabilities == ("text", "text.chat")
    assert first.resources.cpu_architectures == ("amd64", "x86_64")
    assert first.canonical_json() == second.canonical_json()
    assert first.descriptor_digest == second.descriptor_digest
    assert len(first.registry_key) == 64


def test_sha256_integrity_requires_exact_lowercase_digest() -> None:
    digest = "a" * 64
    descriptor = replace(
        _descriptor(),
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=digest,
        size_bytes=123456,
    )
    assert descriptor.sha256 == digest
    assert descriptor.size_bytes == 123456

    with pytest.raises(ValueError, match="SHA-256"):
        replace(descriptor, sha256=None)
    with pytest.raises(ValueError, match="SHA-256"):
        replace(descriptor, sha256="A" * 64)
    with pytest.raises(ValueError, match="must not claim"):
        replace(_descriptor(), sha256=digest)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_reference", "https://name@example.test/model"),
        ("source_reference", "https://models.example.test/model?variant=x"),
        ("source_reference", "https://models.example.test/model#fragment"),
        ("license_reference", "https://licenses.example.test/model?edition=one"),
        ("source_reference", "env:MODEL_SOURCE_REFERENCE"),
        ("license_reference", "credential:MODEL_LICENSE_REFERENCE"),
        ("source_reference", "file:///home/user/private-model.bin"),
        ("source_reference", "/home/user/private-model.bin"),
        ("source_reference", "C:\\Users\\User\\private-model.bin"),
        ("source_reference", "token=MODEL_CANARY"),
        ("source_reference", "api_key=MODEL_CANARY"),
        ("source_reference", "Authorization: Bearer MODEL_CANARY"),
        ("license_reference", "Cookie: session=MODEL_CANARY"),
        ("license_reference", "password = MODEL_CANARY"),
    ),
)
def test_public_references_reject_ambiguous_url_surfaces(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        replace(_descriptor(), **{field: value})


@pytest.mark.parametrize(
    "value",
    (
        "token=MODEL_CANARY",
        "api_key=MODEL_CANARY",
        "Authorization: Bearer MODEL_CANARY",
        "Cookie: session=MODEL_CANARY",
        "password = MODEL_CANARY",
    ),
)
def test_rejected_secret_reference_does_not_echo_canary(value: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        replace(_descriptor(), source_reference=value)
    assert "MODEL_CANARY" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider_id", " foundry-local"),
        ("provider_id", "foundry/local"),
        ("model_id", "model\nother"),
        ("model_version", " 7"),
        ("source_reference", ""),
        ("license_reference", " reviewed "),
    ),
)
def test_identity_text_is_bounded_and_unambiguous(field: str, value: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_descriptor(), **{field: value})


@pytest.mark.parametrize(
    "resources",
    (
        ModelArtifactResources(min_system_memory_bytes=None),
        ModelArtifactResources(min_vram_bytes=1),
    ),
)
def test_valid_resource_requirements_round_trip(resources: ModelArtifactResources) -> None:
    descriptor = replace(_descriptor(), resources=resources)
    restored = ModelArtifactDescriptor.from_json(descriptor.canonical_json())
    assert restored == descriptor


def test_resource_requirements_reject_bool_huge_and_inverted_values() -> None:
    with pytest.raises(TypeError, match="integer"):
        ModelArtifactResources(min_system_memory_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="range"):
        ModelArtifactResources(min_system_memory_bytes=1 << 80)
    with pytest.raises(ValueError, match="recommended_memory_bytes"):
        ModelArtifactResources(
            min_system_memory_bytes=16 * 1024**3,
            recommended_memory_bytes=8 * 1024**3,
        )
    with pytest.raises(ValueError, match="architecture"):
        ModelArtifactResources(cpu_architectures=("amd64/other",))


def test_registration_is_durable_restart_safe_and_audit_safe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ModelArtifactRegistry(store)
    descriptor = _descriptor(model_id="модель-для-Nika:7")

    digest = registry.register(descriptor)
    restarted = ModelArtifactRegistry(SQLiteStore(store.path))
    restored = restarted.get(descriptor.provider_id, descriptor.model_id)

    assert restored == descriptor
    assert digest == descriptor.descriptor_digest
    assert restarted.list() == (descriptor,)

    events = AuditLog(SQLiteStore(store.path)).list_for(
        entity_type="model_artifact",
        entity_id=descriptor.registry_key,
    )
    assert [event.event_type for event in events] == ["model.artifact.registered"]
    rendered = json.dumps([event.payload for event in events], sort_keys=True)
    assert descriptor.source_reference not in rendered
    assert descriptor.license_reference not in rendered
    assert descriptor.model_id not in rendered
    assert descriptor.provider_id in rendered


def test_identical_registration_is_idempotent_without_duplicate_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ModelArtifactRegistry(store)
    descriptor = _descriptor()

    first = registry.register(descriptor)
    second = registry.register(descriptor)

    assert first == second
    events = AuditLog(store).list_for(
        entity_type="model_artifact",
        entity_id=descriptor.registry_key,
    )
    assert len(events) == 1


def test_same_provider_model_identity_cannot_change_provenance(tmp_path: Path) -> None:
    registry = ModelArtifactRegistry(_store(tmp_path))
    descriptor = _descriptor()
    registry.register(descriptor)

    with pytest.raises(ModelArtifactConflictError, match="different provenance"):
        registry.register(
            replace(
                descriptor,
                license_reference="https://licenses.example.test/models/reviewed-v2",
            )
        )

    assert registry.get(descriptor.provider_id, descriptor.model_id) == descriptor


def test_concurrent_identical_writers_collapse_to_one_durable_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    left = ModelArtifactRegistry(store)
    right = ModelArtifactRegistry(store)
    descriptor = _descriptor()
    barrier = Barrier(2)

    def register(registry: ModelArtifactRegistry) -> str:
        barrier.wait(timeout=5)
        return registry.register(descriptor)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(register, left),
            executor.submit(register, right),
        )
        results = [future.result(timeout=10) for future in futures]

    assert results == [descriptor.descriptor_digest, descriptor.descriptor_digest]
    assert left.list() == (descriptor,)
    events = AuditLog(store).list_for(
        entity_type="model_artifact",
        entity_id=descriptor.registry_key,
    )
    assert len(events) == 1


def test_concurrent_conflicting_writers_allow_exactly_one_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    left = ModelArtifactRegistry(store)
    right = ModelArtifactRegistry(store)
    first = _descriptor(
        license_reference="https://licenses.example.test/models/review-a"
    )
    second = replace(
        first,
        license_reference="https://licenses.example.test/models/review-b",
    )
    barrier = Barrier(2)

    def register(
        registry: ModelArtifactRegistry,
        descriptor: ModelArtifactDescriptor,
    ) -> str | type[Exception]:
        barrier.wait(timeout=5)
        try:
            return registry.register(descriptor)
        except Exception as exc:  # noqa: BLE001 - test records which writer lost.
            return type(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(register, left, first),
            executor.submit(register, right, second),
        )
        results = [future.result(timeout=10) for future in futures]

    assert sum(isinstance(value, str) for value in results) == 1
    assert results.count(ModelArtifactConflictError) == 1
    stored = left.get(first.provider_id, first.model_id)
    assert stored in {first, second}


def test_corrupt_durable_descriptor_digest_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ModelArtifactRegistry(store)
    descriptor = _descriptor()
    registry.register(descriptor)

    with store.connection() as conn:
        conn.execute(
            "UPDATE model_artifacts SET descriptor_digest = ? "
            "WHERE provider_id = ? AND model_id = ?",
            ("0" * 64, descriptor.provider_id, descriptor.model_id),
        )

    with pytest.raises(ModelArtifactRegistryError, match="digest mismatch"):
        registry.get(descriptor.provider_id, descriptor.model_id)


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    (
        ("provider_id", "foreign-provider"),
        ("model_id", "foreign-model"),
    ),
)
def test_corrupt_durable_row_key_identity_fails_closed(
    tmp_path: Path,
    column: str,
    corrupt_value: str,
) -> None:
    store = _store(tmp_path)
    registry = ModelArtifactRegistry(store)
    descriptor = _descriptor()
    registry.register(descriptor)

    with store.connection() as conn:
        if column == "provider_id":
            conn.execute(
                "UPDATE model_artifacts SET provider_id = ? "
                "WHERE provider_id = ? AND model_id = ?",
                (corrupt_value, descriptor.provider_id, descriptor.model_id),
            )
            provider_id = corrupt_value
            model_id = descriptor.model_id
        else:
            conn.execute(
                "UPDATE model_artifacts SET model_id = ? "
                "WHERE provider_id = ? AND model_id = ?",
                (corrupt_value, descriptor.provider_id, descriptor.model_id),
            )
            provider_id = descriptor.provider_id
            model_id = corrupt_value

    with pytest.raises(ModelArtifactRegistryError, match="identity mismatch"):
        registry.get(provider_id, model_id)
    with pytest.raises(ModelArtifactRegistryError, match="identity mismatch"):
        registry.list()

    rebound = replace(descriptor, **{column: corrupt_value})
    with pytest.raises(ModelArtifactRegistryError, match="identity mismatch"):
        registry.register(rebound)


def test_unknown_artifact_is_not_synthesized(tmp_path: Path) -> None:
    registry = ModelArtifactRegistry(_store(tmp_path))

    with pytest.raises(KeyError):
        registry.get("ollama", "not-installed")


def test_direct_deserialization_rejects_non_list_set_like_fields() -> None:
    raw = _descriptor().as_dict()
    raw["capabilities"] = "text.chat"
    with pytest.raises(ModelArtifactRegistryError, match="capabilities"):
        ModelArtifactDescriptor.from_json(json.dumps(raw))

    raw = _descriptor().as_dict()
    resources = dict(raw["resources"])  # type: ignore[arg-type]
    resources["cpu_architectures"] = "amd64"
    raw["resources"] = resources
    with pytest.raises(ModelArtifactRegistryError, match="cpu architectures"):
        ModelArtifactDescriptor.from_json(json.dumps(raw))


def test_direct_constructor_rejects_string_instead_of_tuple_metadata() -> None:
    with pytest.raises(TypeError, match="capabilities"):
        replace(_descriptor(), capabilities="text.chat")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cpu_architectures"):
        replace(
            _descriptor(),
            resources=ModelArtifactResources(
                cpu_architectures="amd64",  # type: ignore[arg-type]
            ),
        )
