from __future__ import annotations

import math
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from nika_core.media.contracts import EngineDescriptor, FrozenModel, ModelDescriptor
from nika_core.media.hashing import sha256_file
from nika_core.resources.contracts import ResourceObserverPort


class BinaryEvidence(FrozenModel):
    component_id: str = Field(min_length=1, max_length=120)
    engine_id: str = Field(min_length=1, max_length=160)
    path_name: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    source_reference: str = Field(min_length=1, max_length=1000)
    license_classification: str = Field(min_length=1, max_length=300)


class ModelEvidence(FrozenModel):
    model_id: str = Field(min_length=1, max_length=160)
    engine_id: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    license_reference: str = Field(min_length=1, max_length=1000)
    source_reference: str = Field(min_length=1, max_length=1000)


class EngineExecutionEvidence(FrozenModel):
    engine_id: str = Field(min_length=1, max_length=160)
    evidence_kind: Literal["probe", "ocr"]
    fixture_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class ResourceMeasurementSample(FrozenModel):
    ordinal: int = Field(ge=1, le=60)
    cpu_percent: float = Field(ge=0.0, le=100.0)
    memory_percent: float = Field(ge=0.0, le=100.0)
    available_memory_bytes: int = Field(ge=0)

    @field_validator("cpu_percent", "memory_percent")
    @classmethod
    def finite_percent(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("resource percentages must be finite")
        return value


class TargetMachineResourceEvidence(FrozenModel):
    machine_label: str = Field(min_length=1, max_length=160)
    platform_system: str = Field(min_length=1, max_length=120)
    platform_release: str = Field(min_length=1, max_length=240)
    machine_architecture: str = Field(min_length=1, max_length=120)
    python_version: str = Field(min_length=1, max_length=120)
    samples: tuple[ResourceMeasurementSample, ...] = Field(min_length=1, max_length=60)
    operator_attested_target: bool = False

    @model_validator(mode="after")
    def validate_sample_order(self) -> TargetMachineResourceEvidence:
        expected = tuple(range(1, len(self.samples) + 1))
        actual = tuple(item.ordinal for item in self.samples)
        if actual != expected:
            raise ValueError("resource measurement samples must use contiguous ordinals")
        return self


class MediaProofManifest(FrozenModel):
    schema_version: int = 4
    engines: tuple[EngineDescriptor, ...] = ()
    binaries: tuple[BinaryEvidence, ...] = ()
    models: tuple[ModelEvidence, ...] = ()
    executions: tuple[EngineExecutionEvidence, ...] = ()
    resource_measurement: TargetMachineResourceEvidence | None = None
    real_engine_execution_proven: bool = False
    target_machine_measured: bool = False

    @model_validator(mode="after")
    def validate_identity(self) -> MediaProofManifest:
        engines_by_id = {item.engine_id: item for item in self.engines}
        if len(engines_by_id) != len(self.engines):
            raise ValueError("engine evidence IDs must be unique")
        binaries_by_component = {item.component_id: item for item in self.binaries}
        if len(binaries_by_component) != len(self.binaries):
            raise ValueError("binary evidence component IDs must be unique")
        binaries_by_engine = {item.engine_id: item for item in self.binaries}
        if len(binaries_by_engine) != len(self.binaries):
            raise ValueError("binary evidence engine IDs must be unique")
        model_ids = {item.model_id for item in self.models}
        if len(model_ids) != len(self.models):
            raise ValueError("model evidence IDs must be unique")
        execution_ids = {item.engine_id for item in self.executions}
        if len(execution_ids) != len(self.executions):
            raise ValueError("engine execution evidence IDs must be unique")
        engine_ids = set(engines_by_id)
        binary_engine_ids = set(binaries_by_engine)
        if not binary_engine_ids.issubset(engine_ids):
            raise ValueError("binary evidence must reference a proven engine")
        for engine_id, binary in binaries_by_engine.items():
            descriptor = engines_by_id[engine_id]
            if descriptor.executable_sha256 is None:
                raise ValueError(
                    "binary evidence requires the matching engine descriptor executable checksum"
                )
            if descriptor.executable_sha256 != binary.sha256:
                raise ValueError("binary evidence checksum must match the engine descriptor")
        for model in self.models:
            if model.engine_id not in engine_ids:
                raise ValueError("model evidence must reference a proven engine")
        if not execution_ids.issubset(engine_ids):
            raise ValueError("engine execution evidence must reference a proven engine")
        if self.real_engine_execution_proven:
            if execution_ids != engine_ids:
                raise ValueError(
                    "full real-engine proof requires execution evidence for every declared engine"
                )
            if binary_engine_ids != engine_ids:
                raise ValueError(
                    "full real-engine proof requires audited binary evidence for every declared engine"
                )
        attested_measurement = bool(
            self.resource_measurement is not None
            and self.resource_measurement.operator_attested_target
        )
        if self.target_machine_measured != attested_measurement:
            raise ValueError(
                "target_machine_measured requires an operator-attested resource measurement"
            )
        return self


def binary_evidence(
    *,
    component_id: str,
    engine_id: str,
    path: Path,
    source_reference: str,
    license_classification: str,
) -> BinaryEvidence:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("binary evidence path must be a regular file")
    return BinaryEvidence(
        component_id=component_id,
        engine_id=engine_id,
        path_name=resolved.name,
        sha256=sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
        source_reference=source_reference,
        license_classification=license_classification,
    )


def model_evidence(
    *,
    descriptor: ModelDescriptor,
    path: Path,
    source_reference: str,
) -> ModelEvidence:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("model evidence path must be a regular file")
    digest = sha256_file(resolved)
    if descriptor.sha256 is not None and descriptor.sha256 != digest:
        raise ValueError("model descriptor checksum does not match the supplied model file")
    size = resolved.stat().st_size
    if descriptor.size_bytes is not None and descriptor.size_bytes != size:
        raise ValueError("model descriptor size does not match the supplied model file")
    return ModelEvidence(
        model_id=descriptor.model_id,
        engine_id=descriptor.engine_id,
        version=descriptor.version,
        sha256=digest,
        size_bytes=size,
        license_reference=descriptor.license_reference,
        source_reference=source_reference,
    )


def resource_evidence(
    *,
    observer: ResourceObserverPort,
    machine_label: str,
    operator_attested_target: bool,
    sample_count: int = 5,
    sample_interval_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> TargetMachineResourceEvidence:
    if sample_count < 1 or sample_count > 60:
        raise ValueError("sample_count must be between 1 and 60")
    if not math.isfinite(sample_interval_seconds) or sample_interval_seconds < 0:
        raise ValueError("sample_interval_seconds must be finite and non-negative")

    samples: list[ResourceMeasurementSample] = []
    for index in range(sample_count):
        snapshot = observer.snapshot()
        samples.append(
            ResourceMeasurementSample(
                ordinal=index + 1,
                cpu_percent=snapshot.cpu_percent,
                memory_percent=snapshot.memory_percent,
                available_memory_bytes=snapshot.available_memory_bytes,
            )
        )
        if index + 1 < sample_count and sample_interval_seconds:
            sleep(sample_interval_seconds)

    return TargetMachineResourceEvidence(
        machine_label=machine_label,
        platform_system=platform.system() or "unknown",
        platform_release=platform.release() or "unknown",
        machine_architecture=platform.machine() or "unknown",
        python_version=sys.version.split()[0],
        samples=tuple(samples),
        operator_attested_target=operator_attested_target,
    )