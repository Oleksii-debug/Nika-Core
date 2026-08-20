from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator

from nika_core.media.contracts import EngineDescriptor, FrozenModel, ModelDescriptor
from nika_core.media.hashing import sha256_file


class BinaryEvidence(FrozenModel):
    component_id: str = Field(min_length=1, max_length=120)
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


class MediaProofManifest(FrozenModel):
    schema_version: int = 1
    engines: tuple[EngineDescriptor, ...] = ()
    binaries: tuple[BinaryEvidence, ...] = ()
    models: tuple[ModelEvidence, ...] = ()
    real_engine_execution_proven: bool = False
    target_machine_measured: bool = False

    @model_validator(mode="after")
    def validate_identity(self) -> MediaProofManifest:
        engine_ids = {item.engine_id for item in self.engines}
        if len(engine_ids) != len(self.engines):
            raise ValueError("engine evidence IDs must be unique")
        binary_ids = {item.component_id for item in self.binaries}
        if len(binary_ids) != len(self.binaries):
            raise ValueError("binary evidence component IDs must be unique")
        model_ids = {item.model_id for item in self.models}
        if len(model_ids) != len(self.models):
            raise ValueError("model evidence IDs must be unique")
        for model in self.models:
            if model.engine_id not in engine_ids:
                raise ValueError("model evidence must reference a proven engine")
        return self


def binary_evidence(
    *,
    component_id: str,
    path: Path,
    source_reference: str,
    license_classification: str,
) -> BinaryEvidence:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("binary evidence path must be a regular file")
    return BinaryEvidence(
        component_id=component_id,
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
