from __future__ import annotations

import json
from collections.abc import Callable, Iterable
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
    """One numeric metric with explicit observed-vs-UNKNOWN semantics."""

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
    """Project benchmark snapshots into explicit resource evidence.

    ResourceObserverPort currently exposes host CPU/RAM plus optional Nika-process
    RSS. AcceleratorSnapshot does not attest accelerator kind, so accelerator
    readings are deliberately not relabelled as GPU measurements.
    """

    samples: list[dict[str, Any]] = []
    for result in report.case_results:
        samples.extend(
            (
                _sample_payload(
                    case_id=result.case_id,
                    phase="before",
                    resource=result.resource_before,
                    accelerator=result.accelerator_before,
                ),
                _sample_payload(
                    case_id=result.case_id,
                    phase="after",
                    resource=result.resource_after,
                    accelerator=result.accelerator_after,
                ),
            )
        )

    resource_snapshots = tuple(
        snapshot
        for result in report.case_results
        for snapshot in (result.resource_before, result.resource_after)
        if snapshot is not None
    )

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
            "peak_host_cpu_percent": _summary_metric(
                (snapshot.cpu_percent for snapshot in resource_snapshots),
                scope=ResourceMeasurementScope.HOST,
                unit=ResourceMeasurementUnit.PERCENT,
                aggregate=max,
            ).as_dict(),
            "peak_host_ram_percent": _summary_metric(
                (snapshot.memory_percent for snapshot in resource_snapshots),
                scope=ResourceMeasurementScope.HOST,
                unit=ResourceMeasurementUnit.PERCENT,
                aggregate=max,
            ).as_dict(),
            "min_host_available_ram_bytes": _summary_metric(
                (snapshot.available_memory_bytes for snapshot in resource_snapshots),
                scope=ResourceMeasurementScope.HOST,
                unit=ResourceMeasurementUnit.BYTES,
                aggregate=min,
            ).as_dict(),
            "peak_nika_process_rss_bytes": _summary_metric(
                (
                    snapshot.process_rss_bytes
                    for snapshot in resource_snapshots
                    if snapshot.process_rss_bytes is not None
                ),
                scope=ResourceMeasurementScope.NIKA_PROCESS,
                unit=ResourceMeasurementUnit.BYTES,
                aggregate=max,
            ).as_dict(),
            "peak_gpu_utilization_percent": _unknown(
                ResourceMeasurementScope.GPU_DEVICE,
                ResourceMeasurementUnit.PERCENT,
                ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
            ).as_dict(),
            "peak_gpu_memory_used_bytes": _unknown(
                ResourceMeasurementScope.GPU_DEVICE,
                ResourceMeasurementUnit.BYTES,
                ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
            ).as_dict(),
        },
        "samples": samples,
    }


def benchmark_resource_evidence_json(report: CandidateBenchmarkReport) -> str:
    return json.dumps(
        benchmark_resource_evidence_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
        available_ram = _unknown(
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
        available_ram = _observed(
            resource.available_memory_bytes,
            ResourceMeasurementScope.HOST,
            ResourceMeasurementUnit.BYTES,
        )
        rss = (
            _observed(
                resource.process_rss_bytes,
                ResourceMeasurementScope.NIKA_PROCESS,
                ResourceMeasurementUnit.BYTES,
            )
            if resource.process_rss_bytes is not None
            else _unknown(
                ResourceMeasurementScope.NIKA_PROCESS,
                ResourceMeasurementUnit.BYTES,
                ResourceUnknownReason.METRIC_UNAVAILABLE,
            )
        )

    return {
        "case_id": case_id,
        "phase": phase,
        "host_cpu_percent": cpu.as_dict(),
        "host_ram_percent": ram.as_dict(),
        "host_available_ram_bytes": available_ram.as_dict(),
        "nika_process_rss_bytes": rss.as_dict(),
        "gpu_utilization_percent": _unknown(
            ResourceMeasurementScope.GPU_DEVICE,
            ResourceMeasurementUnit.PERCENT,
            ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
        ).as_dict(),
        "gpu_memory_used_bytes": _unknown(
            ResourceMeasurementScope.GPU_DEVICE,
            ResourceMeasurementUnit.BYTES,
            ResourceUnknownReason.GPU_IDENTITY_UNAVAILABLE,
        ).as_dict(),
        "untyped_accelerator_observation_present": accelerator is not None,
    }


def _observed(
    value: float | int,
    scope: ResourceMeasurementScope,
    unit: ResourceMeasurementUnit,
) -> ResourceMetricEvidence:
    return ResourceMetricEvidence(
        status=ResourceMeasurementStatus.OBSERVED,
        scope=scope,
        unit=unit,
        value=value,
    )


def _unknown(
    scope: ResourceMeasurementScope,
    unit: ResourceMeasurementUnit,
    reason: ResourceUnknownReason,
) -> ResourceMetricEvidence:
    return ResourceMetricEvidence(
        status=ResourceMeasurementStatus.UNKNOWN,
        scope=scope,
        unit=unit,
        value=None,
        unknown_reason=reason,
    )


def _summary_metric(
    values: Iterable[float | int],
    *,
    scope: ResourceMeasurementScope,
    unit: ResourceMeasurementUnit,
    aggregate: Callable[[Iterable[float | int]], float | int],
) -> ResourceMetricEvidence:
    observed = tuple(values)
    if not observed:
        return _unknown(
            scope,
            unit,
            ResourceUnknownReason.NO_OBSERVED_SAMPLE,
        )
    return _observed(aggregate(observed), scope, unit)
