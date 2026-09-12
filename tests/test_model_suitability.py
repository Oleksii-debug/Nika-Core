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
    ModelSuitability,
    RuntimeSuitabilityConstraints,
)
from nika_core.resources.contracts import ResourceSnapshot

_GIB = 1024**3


class FakeObserver:
    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> ResourceSnapshot:
        self.calls += 1
        return self._snapshot


def _snapshot(
    *,
    total_memory_bytes: int | None = 32 * _GIB,
    available_memory_bytes: int = 20 * _GIB,
    logical_cpu_count: int | None = 12,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_percent=12.5,
        memory_percent=37.5,
        available_memory_bytes=available_memory_bytes,
        logical_cpu_count=logical_cpu_count,
        total_memory_bytes=total_memory_bytes,
    )


def _descriptor(
    *,
    resources: ModelArtifactResources | None = None,
    size_bytes: int | None = 8 * _GIB,
    kind: ModelArtifactKind = ModelArtifactKind.EMBEDDED,
) -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=kind,
        provider_id="foundry-local",
        model_id="example-model-v1",
        source_reference="https://example.invalid/models/example-model-v1",
        license_reference="https://example.invalid/licenses/example-model-v1",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        size_bytes=size_bytes,
        resources=resources
        or ModelArtifactResources(
            min_system_memory_bytes=8 * _GIB,
            min_available_memory_bytes=4 * _GIB,
            min_vram_bytes=6 * _GIB,
            recommended_memory_bytes=16 * _GIB,
            cpu_architectures=("x86_64",),
        ),
    )


def _evaluator(
    snapshot: ResourceSnapshot,
    *,
    disk_free_bytes: int = 40 * _GIB,
    architecture: str = "AMD64",
) -> tuple[EmbeddedModelSuitabilityEvaluator, FakeObserver]:
    observer = FakeObserver(snapshot)
    evaluator = EmbeddedModelSuitabilityEvaluator(
        observer,
        disk_free_probe=lambda _path: disk_free_bytes,
        architecture_probe=lambda: architecture,
    )
    return evaluator, observer


def test_supported_profile_is_machine_readable_and_observed_once() -> None:
    evaluator, observer = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(),
        storage_path=Path("unused-by-fake"),
        model_cached=False,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
        constraints=RuntimeSuitabilityConstraints(
            min_logical_cpu_count=4,
            max_estimated_runtime_ms=2_000.0,
        ),
        estimated_runtime_ms=1_250.0,
    )

    assert report.classification is ModelSuitability.SUPPORTED
    assert report.reason_codes == ("declared_requirements_satisfied",)
    assert observer.calls == 1
    payload = report.as_dict()
    assert payload["schema"] == "nika.embedded_model_suitability.v1"
    assert payload["classification"] == "SUPPORTED"
    assert payload["reason_codes"] == ["declared_requirements_satisfied"]
    assert payload["observations"]["cpu_architecture"] == "x86_64"
    assert payload["observations"]["estimated_runtime_ms"] == 1_250.0


def test_insufficient_system_memory_is_unsuitable() -> None:
    evaluator, _ = _evaluator(_snapshot(total_memory_bytes=4 * _GIB))

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
    )

    assert report.classification is ModelSuitability.UNSUITABLE
    assert "insufficient_system_memory" in report.reason_codes


def test_required_gpu_with_unknown_gpu_evidence_is_unknown() -> None:
    evaluator, _ = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
    )

    assert report.classification is ModelSuitability.UNKNOWN
    assert report.reason_codes == ("gpu_availability_unknown",)


def test_required_gpu_without_enough_vram_is_unsuitable() -> None:
    evaluator, _ = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=2 * _GIB),
    )

    assert report.classification is ModelSuitability.UNSUITABLE
    assert report.reason_codes == ("insufficient_vram",)


def test_cpu_architecture_mismatch_is_unsuitable() -> None:
    evaluator, _ = _evaluator(_snapshot(), architecture="ARM64")

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
    )

    assert report.classification is ModelSuitability.UNSUITABLE
    assert report.reason_codes == ("cpu_architecture_unsupported",)


def test_uncached_model_without_enough_disk_is_unsuitable_without_downloading() -> None:
    evaluator, _ = _evaluator(_snapshot(), disk_free_bytes=4 * _GIB)

    report = evaluator.assess(
        _descriptor(size_bytes=8 * _GIB),
        storage_path="model-cache",
        model_cached=False,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
    )

    assert report.classification is ModelSuitability.UNSUITABLE
    assert report.reason_codes == ("insufficient_disk_for_model",)
    assert report.observations.model_cached is False
    assert report.observations.disk_free_bytes == 4 * _GIB


def test_uncached_model_with_unknown_size_is_unknown() -> None:
    evaluator, _ = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(size_bytes=None),
        storage_path="model-cache",
        model_cached=False,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
    )

    assert report.classification is ModelSuitability.UNKNOWN
    assert report.reason_codes == ("model_size_unknown",)


def test_runtime_limit_without_external_estimate_is_maybe_not_fabricated() -> None:
    evaluator, _ = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
        constraints=RuntimeSuitabilityConstraints(max_estimated_runtime_ms=2_000.0),
    )

    assert report.classification is ModelSuitability.MAYBE
    assert report.reason_codes == ("runtime_estimate_unavailable",)
    assert report.observations.estimated_runtime_ms is None


def test_runtime_estimate_over_declared_limit_is_unsuitable() -> None:
    evaluator, _ = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
        accelerator=AcceleratorEvidence(gpu_available=True, available_vram_bytes=12 * _GIB),
        constraints=RuntimeSuitabilityConstraints(max_estimated_runtime_ms=2_000.0),
        estimated_runtime_ms=3_000.0,
    )

    assert report.classification is ModelSuitability.UNSUITABLE
    assert report.reason_codes == ("estimated_runtime_exceeds_limit",)


def test_below_recommended_memory_is_maybe_after_hard_minimums_pass() -> None:
    resources = ModelArtifactResources(
        min_system_memory_bytes=8 * _GIB,
        min_available_memory_bytes=2 * _GIB,
        recommended_memory_bytes=24 * _GIB,
        cpu_architectures=("amd64",),
    )
    evaluator, _ = _evaluator(_snapshot(total_memory_bytes=16 * _GIB))

    report = evaluator.assess(
        _descriptor(resources=resources),
        storage_path="unused",
        model_cached=True,
    )

    assert report.classification is ModelSuitability.MAYBE
    assert report.reason_codes == ("below_recommended_memory",)


def test_missing_declared_hardware_requirements_is_unknown_not_supported() -> None:
    evaluator, _ = _evaluator(_snapshot())

    report = evaluator.assess(
        _descriptor(resources=ModelArtifactResources()),
        storage_path="unused",
        model_cached=True,
    )

    assert report.classification is ModelSuitability.UNKNOWN
    assert report.reason_codes == ("hardware_requirements_not_declared",)


def test_non_embedded_descriptor_is_rejected() -> None:
    evaluator, observer = _evaluator(_snapshot())

    with pytest.raises(ValueError, match="embedded model descriptor"):
        evaluator.assess(
            _descriptor(kind=ModelArtifactKind.EXTERNAL_LOCAL),
            storage_path="unused",
            model_cached=True,
        )

    assert observer.calls == 0


def test_extreme_runtime_integers_fail_closed_without_overflow() -> None:
    huge = 10**10000

    with pytest.raises(ValueError, match="finite"):
        RuntimeSuitabilityConstraints(max_estimated_runtime_ms=huge)

    evaluator, observer = _evaluator(_snapshot())
    with pytest.raises(ValueError, match="finite"):
        evaluator.assess(
            _descriptor(),
            storage_path="unused",
            model_cached=True,
            estimated_runtime_ms=huge,
        )

    assert observer.calls == 0
