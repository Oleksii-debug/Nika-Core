from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from nika_core.model_engineering.contracts import (
    AcceleratorSnapshot,
    CandidateBenchmarkReport,
)
from nika_core.resources.contracts import ResourceSnapshot


class ResourceMeasurementStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class ResourceMeasurementScope(StrEnum):
    HOST = "host"
    NIKA_PROCESS = "nika_process"
    GPU_DEVICE = "gpu_device"


class ResourceMeasurementUnit(StrEnum):
    PERCENT = "percent"
    BYTES = "bytes"


class ResourceUnknownReason(StrEnum):
    RESOURCE_OBSERVER_NOT_CONFIGURED = "resource_observer_not_configured"
    METRIC_UNAVAILABLE = "metric_unavailable"
    GPU_IDENTITY_UNAVAILABLE = "gpu_identity_unavailable"
    NO_OBSERVED_SAMPLE = "no_observed_sample"


@dataclass(frozen=True, slots=True)
class ResourceMetricEvidence:
    """One resource metric with explicit observed-vs-UNKNOWN semantics."""

    status: ResourceMeasurementStatus
    scope: ResourceMeasurementScope
    unit: ResourceMeasurementUnit
    value: float | int | None
    unknown_reason: ResourceUnknownReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResourceMeasurementStatus):
            raise ValueError("status must be a ResourceMeasurementStatus")
        if not isinstance(self.scope, ResourceMeasurementScope):
            raise ValueError("scope must be a ResourceMeasurementScope")
        if not isinstance(self.unit, ResourceMeasurementUnit):
            raise ValueError("unit must be a ResourceMeasurementUnit")
        if self.status is ResourceMeasurementStatus.UNKNOWN:
            if self.value is not None:
                raise ValueError("unknown resource metric cannot carry a numeric value")
            if not isinstance(self.unknown_reason, ResourceUnknownReason):
                raise ValueError("unknown resource metric requires unknown_reason")
            return
        if self.unknown_reason is not None:
            raise ValueError("observed resource metric cannot carry unknown_reason")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("observed resource metric requires a numeric value")
        number = float(self.value)
        if not isfinite(number) or number < 0:
            raise ValueError("observed resource metric must be finite and non-negative")
        if self.unit is ResourceMeasurementUnit.PERCENT and number > 100:
            raise ValueError("observed percent metric must be in [0, 100]")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "scope": self.scope.value,
            "unit": self.unit.value,
            "value": self.value,
            "unknown_reason": (
                self.unknown_reason.value if self.unknown_reason is not None else None
            ),
        }


def benchmark_resource_evidence_payload(
    report: CandidateBenchmarkReport,
) -> dict[str, Any]:
    """Return bounded resource evidence without fabricating GPU attribution."""

    snapshots = tuple(
        snapshot
        for result in report.case_results
        for snapshot in (result.resource_before, result.resource_after)
        if snapshot is not None
    )
    rss_values = tuple(
        value
        for snapshot in snapshots
        if (value := getattr(snapshot, "process_rss_bytes", None)) is not None
    )
    samples = [
        _sample_payload(
            case_id=result.case_id,
            phase=phase,
            resource=resource,
            accelerator=accelerator,
        )
        for result in report.case_results
        for phase, resource, accelerator in (
            ("before", result.resource_before, result.accelerator_before),
            ("after", result.resource_after, result.accelerator_after),
        )
    ]
    return {
        "schema": "nika-model-resource-evidence-v1",
        "candidate_id": report.candidate.candidate_id,
        "evaluation_set_sha256": report.evaluation_set_sha256,
        "sampling": {
            "mode": "bounded_point_snapshots",
            "capture_points": ["before", "after"],
            "during": "not_captured",
        },
        "summary": {
            "peak_host_cpu_percent": _summary(
                (item.cpu_percent for item in snapshots),
                ResourceMeasurementScope.HOST,
                ResourceMeasurementUnit.PERCENT,
            ),
            "peak_host_ram_percent": _summary(
                (item.memory_percent for item in snapshots),
                ResourceMeasurementScope.HOST,
                ResourceMeasurementUnit.PERCENT,
            ),
            "min_host_available_ram_bytes": _summary(
                (item.available_memory_bytes for item in snapshots),
                ResourceMeasurementScope.HOST,
                ResourceMeasurementUnit.BYTES,
                minimum=True,
            ),
            "peak_nika_process_rss_bytes": _summary(
                rss_values,
                ResourceMeasurementScope.NIKA_PROCESS,
                ResourceMeasurementUnit.BYTES,
            ),
            "peak_gpu_utilization_percent": _unknown(
                ResourceMeasurementScope.GPU_DEVICE,
                ResourceMeasurementUnit.PERCENT,
                ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
            ),
            "peak_gpu_memory_used_bytes": _unknown(
                ResourceMeasurementScope.GPU_DEVICE,
                ResourceMeasurementUnit.BYTES,
                ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
            ),
        },
        "samples": samples,
    }


def _sample_payload(
    *,
    case_id: str,
    phase: str,
    resource: ResourceSnapshot | None,
    accelerator: AcceleratorSnapshot | None,
) -> dict[str, Any]:
    if resource is None:
        cpu = _unknown(
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.PERCENT,
            ResourceUnknownReason.RESOURCE_OBSERVER_NOT_CONFIGURED,
        )
        ram = _unknown(
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.PERCENT,
            ResourceUnknownReason.RESOURCE_OBSERVER_NOT_CONFIGURED,
        )
        available = _unknown(
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.BYTES,
            ResourceUnknownReason.RESOURCE_OBSERVER_NOT_CONFIGURED,
        )
        rss = _unknown(
            ResourceMeasurementScope.NIKA_PROCESS,
            ResourceMeasurementUnit.BYTES,
            ResourceUnknownReason.RESOURCE_OBSERVER_NOT_CONFIGURED,
        )
    else:
        cpu = _observed(
            resource.cpu_percent,
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.PERCENT,
        )
        ram = _observed(
            resource.memory_percent,
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.PERCENT,
        )
        available = _observed(
            resource.available_memory_bytes,
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.BYTES,
        )
        rss_value = getattr(resource, "process_rss_bytes", None)
        rss = (
            _observed(
                rss_value,
                ResourceMeasurementScope.NIKA_PROCESS,
                ResourceMeasurementUnit.BYTES,
            )
            if rss_value is not None
            else _unknown(
                ResourceMeasurementScope.NIKA_PROCESS,
                ResourceMeasurementUnit.BYTES,
                ResourceUnknownReason.METRIC_UNAVAILABLE,
            )
        )

    gpu_percent = _unknown(
        ResourceMeasurementScope.GPU_DEVICE,
        ResourceMeasurementUnit.PERCENT,
        ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
    )
    gpu_memory = _unknown(
        ResourceMeasurementScope.GPU_DEVICE,
        ResourceMeasurementUnit.BYTES,
        ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
    )
    return {
        "case_id": case_id,
        "phase": phase,
        "host_cpu_percent": cpu,
        "host_ram_percent": ram,
        "host_available_ram_bytes": available,
        "nika_process_rss_bytes": rss,
        "gpu_utilization_percent": gpu_percent,
        "gpu_memory_used_bytes": gpu_memory,
        "untyped_accelerator_observation_present": accelerator is not None,
    }


def _observed(
    value: float | int,
    scope: ResourceMeasurementScope,
    unit: ResourceMeasurementUnit,
) -> dict[str, Any]:
    return ResourceMetricEvidence(
        ResourceMeasurementStatus.OBSERVED,
        scope,
        unit,
        value,
    ).as_dict()


def _unknown(
    scope: ResourceMeasurementScope,
    unit: ResourceMeasurementUnit,
    reason: ResourceUnknownReason,
) -> dict[str, Any]:
    return ResourceMetricEvidence(
        ResourceMeasurementStatus.UNKNOWN,
        scope,
        unit,
        None,
        reason,
    ).as_dict()


def _summary(
    values: Iterable[float | int],
    scope: ResourceMeasurementScope,
    unit: ResourceMeasurementUnit,
    *,
    minimum: bool = False,
) -> dict[str, Any]:
    observed = tuple(values)
    if not observed:
        return _unknown(
            scope,
            unit,
            ResourceUnknownReason.NO_OBSERVED_SAMPLE,
        )
    value = min(observed) if minimum else max(observed)
    return _observed(value, scope, unit)
