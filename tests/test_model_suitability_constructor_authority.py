from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactResources,
    ModelIntegrityBasis,
)
from nika_core.model_suitability import (
    AcceleratorEvidence,
    EmbeddedModelSuitabilityEvaluator,
    RuntimeSuitabilityConstraints,
)
from nika_core.resources.contracts import ResourceSnapshot

_GIB = 1024**3


class _HostileInt(int):
    def __lt__(self, _other: object) -> bool:
        return False

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


def _descriptor(
    *, resources: ModelArtifactResources | None = None
) -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="foundry-local",
        model_id="constructor-authority-model",
        source_reference="https://example.invalid/model",
        license_reference="https://example.invalid/license",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        size_bytes=1 * _GIB,
        resources=resources
        if resources is not None
        else ModelArtifactResources(min_system_memory_bytes=1 * _GIB),
    )


def _evaluator() -> tuple[
    EmbeddedModelSuitabilityEvaluator,
    _CountingObserver,
    list[Path],
]:
    observer = _CountingObserver()
    disk_calls: list[Path] = []

    def disk_probe(path: Path) -> int:
        disk_calls.append(path)
        return 100 * _GIB

    evaluator = EmbeddedModelSuitabilityEvaluator(
        observer,
        disk_free_probe=disk_probe,
        architecture_probe=lambda: "AMD64",
    )
    return evaluator, observer, disk_calls


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


def test_forged_descriptor_requirement_fails_before_hardware_observation() -> None:
    descriptor = _descriptor()
    object.__setattr__(descriptor, "resources", _forged_resources())
    evaluator, observer, disk_calls = _evaluator()

    with pytest.raises(TypeError, match="exact integer"):
        evaluator.assess(
            descriptor,
            storage_path="unused",
            model_cached=True,
        )

    assert observer.calls == 0
    assert disk_calls == []


def test_forged_accelerator_evidence_fails_before_hardware_observation() -> None:
    descriptor = _descriptor(
        resources=ModelArtifactResources(min_vram_bytes=8 * _GIB)
    )
    accelerator = object.__new__(AcceleratorEvidence)
    object.__setattr__(accelerator, "gpu_available", True)
    object.__setattr__(accelerator, "available_vram_bytes", _HostileInt(128 * _GIB))
    evaluator, observer, disk_calls = _evaluator()

    with pytest.raises(TypeError, match="exact integer"):
        evaluator.assess(
            descriptor,
            storage_path="unused",
            model_cached=True,
            accelerator=accelerator,
        )

    assert observer.calls == 0
    assert disk_calls == []


def test_forged_runtime_constraints_fail_before_hardware_observation() -> None:
    constraints = object.__new__(RuntimeSuitabilityConstraints)
    object.__setattr__(constraints, "min_logical_cpu_count", _HostileInt(128))
    object.__setattr__(constraints, "max_estimated_runtime_ms", None)
    evaluator, observer, disk_calls = _evaluator()

    with pytest.raises(TypeError, match="exact integer"):
        evaluator.assess(
            _descriptor(),
            storage_path="unused",
            model_cached=True,
            constraints=constraints,
        )

    assert observer.calls == 0
    assert disk_calls == []
