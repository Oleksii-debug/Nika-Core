from __future__ import annotations

import json
from pathlib import Path

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactResources,
    ModelIntegrityBasis,
)
from nika_core.model_suitability import (
    EmbeddedModelSuitabilityEvaluator,
    ModelSuitability,
    RuntimeSuitabilityConstraints,
)
from nika_core.resources.contracts import ResourceSnapshot

_GIB = 1024**3


class _OpaqueValue:
    pass


class _HostileInt(int):
    def __lt__(self, _other: object) -> bool:
        raise AssertionError("hostile comparison must not run")

    def __gt__(self, _other: object) -> bool:
        raise AssertionError("hostile comparison must not run")


class _HostileFloat(float):
    def __float__(self) -> float:
        raise AssertionError("hostile float conversion must not run")


class _Observer:
    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> ResourceSnapshot:
        return self._snapshot


def _descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="foundry-local",
        model_id="snapshot-projection-model",
        source_reference="https://example.invalid/model",
        license_reference="https://example.invalid/license",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        size_bytes=1 * _GIB,
        resources=ModelArtifactResources(
            min_system_memory_bytes=1 * _GIB,
            min_available_memory_bytes=1 * _GIB,
            cpu_architectures=("x86_64",),
        ),
    )


def test_noncanonical_snapshot_values_are_not_exported_from_machine_readable_report() -> None:
    snapshot = ResourceSnapshot(
        cpu_percent=_OpaqueValue(),  # type: ignore[arg-type]
        memory_percent=_HostileFloat(25.0),
        available_memory_bytes=_OpaqueValue(),  # type: ignore[arg-type]
        logical_cpu_count=_HostileInt(8),
        total_memory_bytes=_OpaqueValue(),  # type: ignore[arg-type]
    )
    evaluator = EmbeddedModelSuitabilityEvaluator(
        _Observer(snapshot),
        disk_free_probe=lambda _path: 100 * _GIB,
        architecture_probe=lambda: "AMD64",
    )

    report = evaluator.assess(
        _descriptor(),
        storage_path=Path("unused"),
        model_cached=True,
        constraints=RuntimeSuitabilityConstraints(min_logical_cpu_count=4),
    )

    assert report.classification is ModelSuitability.UNKNOWN
    assert "total_memory_unknown" in report.reason_codes
    assert "available_memory_unknown" in report.reason_codes
    assert "logical_cpu_count_unknown" in report.reason_codes
    observations = report.as_dict()["observations"]
    assert observations["cpu_percent"] is None
    assert observations["memory_percent"] is None
    assert observations["available_memory_bytes"] is None
    assert observations["logical_cpu_count"] is None
    assert observations["total_memory_bytes"] is None
    json.dumps(report.as_dict())


def test_valid_snapshot_values_are_projected_to_plain_builtins() -> None:
    snapshot = ResourceSnapshot(
        cpu_percent=10,
        memory_percent=25.5,
        available_memory_bytes=4 * _GIB,
        logical_cpu_count=8,
        total_memory_bytes=8 * _GIB,
    )
    evaluator = EmbeddedModelSuitabilityEvaluator(
        _Observer(snapshot),
        disk_free_probe=lambda _path: 100 * _GIB,
        architecture_probe=lambda: "AMD64",
    )

    report = evaluator.assess(
        _descriptor(),
        storage_path="unused",
        model_cached=True,
        constraints=RuntimeSuitabilityConstraints(min_logical_cpu_count=4),
    )

    assert report.classification is ModelSuitability.SUPPORTED
    observations = report.observations
    assert type(observations.cpu_percent) is float
    assert observations.cpu_percent == 10.0
    assert type(observations.memory_percent) is float
    assert observations.memory_percent == 25.5
    assert type(observations.available_memory_bytes) is int
    assert type(observations.logical_cpu_count) is int
    assert type(observations.total_memory_bytes) is int
    json.dumps(report.as_dict())
