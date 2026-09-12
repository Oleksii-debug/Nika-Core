from __future__ import annotations

import hashlib

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactRegistry,
    ModelArtifactRegistryError,
    ModelArtifactResources,
    ModelIntegrityBasis,
)


class HostileText(str):
    def __format__(self, format_spec: str) -> str:
        return "attacker-controlled-format"

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 1


class HostileInt(int):
    def __eq__(self, other: object) -> bool:
        return True

    def __lt__(self, other: object) -> bool:
        return False

    def __gt__(self, other: object) -> bool:
        return False


def _descriptor(**overrides: object) -> ModelArtifactDescriptor:
    values: dict[str, object] = {
        "kind": ModelArtifactKind.EMBEDDED,
        "provider_id": "provider",
        "model_id": "model",
        "model_version": "v1",
        "source_reference": "https://models.example.test/model",
        "license_reference": "https://licenses.example.test/model",
        "integrity_basis": ModelIntegrityBasis.SHA256,
        "sha256": hashlib.sha256(b"weights").hexdigest(),
        "size_bytes": len(b"weights"),
        "capabilities": ("text",),
        "resources": ModelArtifactResources(cpu_architectures=("x86_64",)),
    }
    values.update(overrides)
    return ModelArtifactDescriptor(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", HostileText("provider")),
        ("model_id", HostileText("model")),
        ("model_version", HostileText("v1")),
        ("source_reference", HostileText("https://models.example.test/model")),
        ("license_reference", HostileText("https://licenses.example.test/model")),
        ("sha256", HostileText("0" * 64)),
    ],
)
def test_descriptor_rejects_text_subclasses_before_identity_use(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _descriptor(**{field: value})


def test_descriptor_rejects_hostile_capability_text_subclass() -> None:
    with pytest.raises(TypeError, match="exact text labels"):
        _descriptor(capabilities=(HostileText("text"),))


def test_resources_reject_hostile_architecture_text_subclass() -> None:
    with pytest.raises(TypeError, match="exact text labels"):
        ModelArtifactResources(cpu_architectures=(HostileText("x86_64"),))


def test_descriptor_and_resources_reject_integer_subclasses() -> None:
    with pytest.raises(TypeError, match="exact integer"):
        _descriptor(size_bytes=HostileInt(7))
    with pytest.raises(TypeError, match="exact integer"):
        ModelArtifactResources(min_system_memory_bytes=HostileInt(1024))


def test_registry_key_matches_canonical_plain_text_identity() -> None:
    descriptor = _descriptor()

    expected = hashlib.sha256(b"provider\x00model").hexdigest()

    assert descriptor.registry_key == expected


def test_registry_rejects_descriptor_subclasses(tmp_path) -> None:
    class DescriptorSubclass(ModelArtifactDescriptor):
        pass

    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    registry = ModelArtifactRegistry(store)
    canonical = _descriptor()
    subclass = DescriptorSubclass(**canonical.as_dict())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="exact ModelArtifactDescriptor"):
        registry.register(subclass)


def test_registry_get_rejects_hostile_identity_text(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    registry = ModelArtifactRegistry(store)

    with pytest.raises(TypeError, match="exact text"):
        registry.get(HostileText("provider"), "model")
    with pytest.raises(TypeError, match="exact text"):
        registry.get("provider", HostileText("model"))


def test_from_json_rejects_text_subclass_even_when_payload_is_valid() -> None:
    body = HostileText(_descriptor().canonical_json())

    with pytest.raises(ModelArtifactRegistryError, match="JSON is invalid"):
        ModelArtifactDescriptor.from_json(body)
