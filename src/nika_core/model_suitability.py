from __future__ import annotations

import math
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nika_core.model_artifacts import ModelArtifactDescriptor, ModelArtifactKind
from nika_core.resources.contracts import ResourceObserverPort

_SCHEMA = "nika.embedded_model_suitability.v1"


class ModelSuitability(StrEnum):
    SUPPORTED = "SUPPORTED"
    MAYBE = "MAYBE"
    UNSUITABLE = "UNSUITABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AcceleratorEvidence:
    """Optional externally observed accelerator facts; this module does not probe a GPU."""

    gpu_available: bool | None = None
    available_vram_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.gpu_available is not None and type(self.gpu_available) is not bool:
            raise TypeError("gpu_available must be bool or None")
        if self.available_vram_bytes is not None:
            if isinstance(self.available_vram_bytes, bool) or not isinstance(
                self.available_vram_bytes, int
            ):
                raise TypeError("available_vram_bytes must be an integer")
            if self.available_vram_bytes < 0:
                raise ValueError("available_vram_bytes must not be negative")
        if self.gpu_available is False and self.available_vram_bytes not in (None, 0):
            raise ValueError("VRAM evidence contradicts gpu_available=false")


@dataclass(frozen=True, slots=True)
class RuntimeSuitabilityConstraints:
    """Caller-owned practical constraints; no runtime estimate is synthesized here."""

    min_logical_cpu_count: int | None = None
    max_estimated_runtime_ms: float | None = None

    def __post_init__(self) -> None:
        if self.min_logical_cpu_count is not None:
            if isinstance(self.min_logical_cpu_count, bool) or not isinstance(
                self.min_logical_cpu_count, int
            ):
                raise TypeError("min_logical_cpu_count must be an integer")
            if self.min_logical_cpu_count <= 0:
                raise ValueError("min_logical_cpu_count must be greater than zero")
        if self.max_estimated_runtime_ms is not None:
            _positive_finite("max_estimated_runtime_ms", self.max_estimated_runtime_ms)


@dataclass(frozen=True, slots=True)
class SuitabilityObservations:
    cpu_architecture: str | None
    logical_cpu_count: int | None
    cpu_percent: float
    total_memory_bytes: int | None
    available_memory_bytes: int
    memory_percent: float
    disk_free_bytes: int | None
    model_size_bytes: int | None
    model_cached: bool
    gpu_available: bool | None
    available_vram_bytes: int | None
    estimated_runtime_ms: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "cpu_architecture": self.cpu_architecture,
            "logical_cpu_count": self.logical_cpu_count,
            "cpu_percent": self.cpu_percent,
            "total_memory_bytes": self.total_memory_bytes,
            "available_memory_bytes": self.available_memory_bytes,
            "memory_percent": self.memory_percent,
            "disk_free_bytes": self.disk_free_bytes,
            "model_size_bytes": self.model_size_bytes,
            "model_cached": self.model_cached,
            "gpu_available": self.gpu_available,
            "available_vram_bytes": self.available_vram_bytes,
            "estimated_runtime_ms": self.estimated_runtime_ms,
        }


@dataclass(frozen=True, slots=True)
class ModelSuitabilityReport:
    classification: ModelSuitability
    reason_codes: tuple[str, ...]
    descriptor_digest: str
    observations: SuitabilityObservations

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "classification": self.classification.value,
            "reason_codes": list(self.reason_codes),
            "descriptor_digest": self.descriptor_digest,
            "observations": self.observations.as_dict(),
        }


DiskFreeProbe = Callable[[Path], int]
ArchitectureProbe = Callable[[], str]


class EmbeddedModelSuitabilityEvaluator:
    """Read-only evaluator over Nika model provenance and resource-observer contracts."""

    def __init__(
        self,
        observer: ResourceObserverPort,
        *,
        disk_free_probe: DiskFreeProbe | None = None,
        architecture_probe: ArchitectureProbe | None = None,
    ) -> None:
        self._observer = observer
        self._disk_free_probe = disk_free_probe or _disk_free_bytes
        self._architecture_probe = architecture_probe or platform.machine

    def assess(
        self,
        descriptor: ModelArtifactDescriptor,
        *,
        storage_path: str | Path,
        model_cached: bool,
        accelerator: AcceleratorEvidence | None = None,
        constraints: RuntimeSuitabilityConstraints | None = None,
        estimated_runtime_ms: float | None = None,
    ) -> ModelSuitabilityReport:
        """Classify practical resource suitability without loading or acquiring a model."""
        if not isinstance(descriptor, ModelArtifactDescriptor):
            raise TypeError("descriptor must be ModelArtifactDescriptor")
        if descriptor.kind is not ModelArtifactKind.EMBEDDED:
            raise ValueError("embedded suitability requires an embedded model descriptor")
        if type(model_cached) is not bool:
            raise TypeError("model_cached must be bool")
        if estimated_runtime_ms is not None:
            _nonnegative_finite("estimated_runtime_ms", estimated_runtime_ms)

        effective_constraints = constraints or RuntimeSuitabilityConstraints()
        evidence = accelerator or AcceleratorEvidence()
        snapshot = self._observer.snapshot()
        architecture = _canonical_architecture(self._architecture_probe())
        disk_free_bytes = self._read_disk_free(Path(storage_path))
        resources = descriptor.resources

        failures: list[str] = []
        unknowns: list[str] = []
        maybes: list[str] = []

        declared_hardware_requirements = any(
            (
                resources.min_system_memory_bytes is not None,
                resources.min_available_memory_bytes is not None,
                resources.min_vram_bytes is not None,
                bool(resources.cpu_architectures),
                effective_constraints.min_logical_cpu_count is not None,
            )
        )
        if not declared_hardware_requirements:
            unknowns.append("hardware_requirements_not_declared")

        if resources.min_system_memory_bytes is not None:
            if not _is_nonnegative_int(snapshot.total_memory_bytes):
                unknowns.append("total_memory_unknown")
            elif snapshot.total_memory_bytes < resources.min_system_memory_bytes:
                failures.append("insufficient_system_memory")

        if resources.min_available_memory_bytes is not None:
            if not _is_nonnegative_int(snapshot.available_memory_bytes):
                unknowns.append("available_memory_unknown")
            elif snapshot.available_memory_bytes < resources.min_available_memory_bytes:
                failures.append("insufficient_available_memory")

        if resources.recommended_memory_bytes is not None:
            if not _is_nonnegative_int(snapshot.total_memory_bytes):
                maybes.append("recommended_memory_unverified")
            elif snapshot.total_memory_bytes < resources.recommended_memory_bytes:
                maybes.append("below_recommended_memory")

        if resources.cpu_architectures:
            if architecture is None:
                unknowns.append("cpu_architecture_unknown")
            else:
                supported_architectures = {
                    _canonical_architecture(value) for value in resources.cpu_architectures
                }
                if architecture not in supported_architectures:
                    failures.append("cpu_architecture_unsupported")

        if effective_constraints.min_logical_cpu_count is not None:
            if not _is_positive_int(snapshot.logical_cpu_count):
                unknowns.append("logical_cpu_count_unknown")
            elif snapshot.logical_cpu_count < effective_constraints.min_logical_cpu_count:
                failures.append("insufficient_logical_cpu_count")

        if resources.min_vram_bytes is not None:
            if evidence.gpu_available is None:
                unknowns.append("gpu_availability_unknown")
            elif evidence.gpu_available is False:
                failures.append("required_gpu_unavailable")
            elif evidence.available_vram_bytes is None:
                unknowns.append("vram_unknown")
            elif evidence.available_vram_bytes < resources.min_vram_bytes:
                failures.append("insufficient_vram")

        if not model_cached:
            if descriptor.size_bytes is None:
                unknowns.append("model_size_unknown")
            elif disk_free_bytes is None:
                unknowns.append("disk_capacity_unknown")
            elif disk_free_bytes < descriptor.size_bytes:
                failures.append("insufficient_disk_for_model")

        if effective_constraints.max_estimated_runtime_ms is not None:
            if estimated_runtime_ms is None:
                maybes.append("runtime_estimate_unavailable")
            elif estimated_runtime_ms > effective_constraints.max_estimated_runtime_ms:
                failures.append("estimated_runtime_exceeds_limit")

        classification, reasons = _classify(failures, unknowns, maybes)
        return ModelSuitabilityReport(
            classification=classification,
            reason_codes=reasons,
            descriptor_digest=descriptor.descriptor_digest,
            observations=SuitabilityObservations(
                cpu_architecture=architecture,
                logical_cpu_count=snapshot.logical_cpu_count,
                cpu_percent=snapshot.cpu_percent,
                total_memory_bytes=snapshot.total_memory_bytes,
                available_memory_bytes=snapshot.available_memory_bytes,
                memory_percent=snapshot.memory_percent,
                disk_free_bytes=disk_free_bytes,
                model_size_bytes=descriptor.size_bytes,
                model_cached=model_cached,
                gpu_available=evidence.gpu_available,
                available_vram_bytes=evidence.available_vram_bytes,
                estimated_runtime_ms=estimated_runtime_ms,
            ),
        )

    def _read_disk_free(self, storage_path: Path) -> int | None:
        try:
            value = self._disk_free_probe(storage_path)
        except OSError:
            return None
        if not _is_nonnegative_int(value):
            return None
        return value


def _classify(
    failures: list[str], unknowns: list[str], maybes: list[str]
) -> tuple[ModelSuitability, tuple[str, ...]]:
    if failures:
        return ModelSuitability.UNSUITABLE, tuple(failures + unknowns + maybes)
    if unknowns:
        return ModelSuitability.UNKNOWN, tuple(unknowns + maybes)
    if maybes:
        return ModelSuitability.MAYBE, tuple(maybes)
    return ModelSuitability.SUPPORTED, ("declared_requirements_satisfied",)


def _canonical_architecture(value: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    canonical = value.strip().lower()
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }.get(canonical, canonical)


def _disk_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _is_nonnegative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_positive_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _positive_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be finite and greater than zero") from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


def _nonnegative_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be finite and non-negative") from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be finite and non-negative")
