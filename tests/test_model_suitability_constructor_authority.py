from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactResources,
    ModelIntegrityBasis,
)
from nika_core.model_suitability import EmbeddedModelSuitabilityEvaluator
from nika_core.resources.contracts import ResourceSnapshot

_GIB = 1024**3


class _HostileInt(int):
    def __gt__(self, _other: object) -> bool:
        return False


class _CountingObserver:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> ResourceSnapshot:
        self.calls += 1
        return ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=50.0,
            available_memory_bytes=2 * _GIB,
            logical_cpu_count=4,
            total_memory_bytes=4 * _GIB,
        )


def _descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="foundry-local",
        model_id="constructor-authority-model",
        source_reference="https://example.invalid/model",
        license_reference="https://example.invalid/license",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        size_bytes=1 * _GIB,
        resources=ModelArtifactResources(min_system_memory_bytes=1 * _GIB),
    )


def _forged_resources() -> ModelArtifactResources:
    forged = object.__new__(ModelArtifactResources)
    object.__setattr__(
        forged,
        "min_system_memory_bytes",
        _HostileInt(128 * _GIB),
    )
    object.__setattr__(forged, "min_available_memory_bytes", None)
    object.__setattr__(forged, "min_vram_bytes", None)
    object.__setattr__(forged, "recommended_memory_bytes", None)
    object.__setattr__(forged, "cpu_architectures", ())
    return forged


def test_forged_hard_requirement_fails_before_observation_or_disk_probe() -> None:
    descriptor = _descriptor()
    object.__setattr__(descriptor, "resources", _forged_resources())
    observer = _CountingObserver()
    disk_calls = 0

    def disk_probe(_path: Path) -> int:
        nonlocal disk_calls
        disk_calls += 1
        return 100 * _GIB

    evaluator = EmbeddedModelSuitabilityEvaluator(
        observer,
        disk_free_probe=disk_probe,
        architecture_probe=lambda: "AMD64",
    )

    with pytest.raises(TypeError, match="exact integer"):
        evaluator.assess(
            descriptor,
            storage_path="unused",
            model_cached=True,
        )

    assert observer.calls == 0
    assert disk_calls == 0
