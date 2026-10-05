from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from nika_core.artifacts import ArtifactRecord, ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.resources import ResourceBudget, ResourceManager
from nika_core.training_adapters import SubprocessTrainingWorker
from nika_core.training_materials import ResolvedTrainingPackage, resolve_training_materials
from nika_core.training_peft_worker import (
    build_trainer_environment,
    build_training_runtime_metadata,
    candidate_artifact_path,
    model_directory_manifest_sha256,
)
from nika_core.training_physical_pilot import (
    PhysicalTrainingPilotError,
    PhysicalTrainingPilotReport,
    run_physical_training_pilot,
    write_physical_training_pilot_report,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRuntime,
)
from nika_core.training_scale import (
    TrainingScaleError,
    TrainingScalePlan,
    TrainingScaleTier,
    authorize_training_scale,
)

_LOG = logging.getLogger(__name__)
_LEGACY_CONFIG_SCHEMA_VERSION = 1
_CONFIG_SCHEMA_VERSION = 2
_CONFIG_MAX_BYTES = 64 * 1024
_FROZEN_PACKAGE_MAX_BYTES = 1024 * 1024
_TRAINER_MAX_RECORDS = 1_000_000
_MAX_TEXT_BYTES = 4096
_TARGET_MODULE_RE = re.compile(r"^[A-Za-z0-9._:+/-]{1,256}$")
_RUNTIME_VERSION_KEYS = frozenset(
    {"torch", "transformers", "peft", "accelerate", "gguf", "safetensors"}
)
_TOP_LEVEL_KEYS_V1 = frozenset(
    {
        "schema_version",
        "workspace_id",
        "project_id",
        "owner_id",
        "job_id",
        "blob_store_root",
        "frozen_package_path",
        "frozen_package_sha256",
        "trainer_executable",
        "base_artifact_ref",
        "base_gguf_path",
        "model_dir",
        "output_root",
        "candidate_artifact_ref",
        "candidate_descriptor",
        "runtime_versions",
        "resource_budget",
        "trainer_parameters",
    }
)
_TOP_LEVEL_KEYS_V2 = _TOP_LEVEL_KEYS_V1 | {"scale_plan"}
_DESCRIPTOR_KEYS = frozenset({"model_id", "source_reference", "license_reference"})
_SCALE_PLAN_KEYS = frozenset({"plan_id", "tiers"})
_SCALE_TIER_KEYS = frozenset(
    {
        "tier_id",
        "max_training_records",
        "max_training_bytes",
        "max_validation_records",
        "max_validation_bytes",
        "max_steps",
    }
)
_MAX_SCALE_VALUE = (1 << 63) - 1
_RESOURCE_KEYS = frozenset({"max_cpu_percent", "max_memory_percent"})
_TRAINER_KEYS = frozenset(
    {
        "max_sequence_length",
        "learning_rate",
        "lora_r",
        "lora_alpha",
        "lora_dropout",
        "lora_target_modules",
        "torch_num_threads",
        "seed",
    }
)


class PhysicalPilotDriverError(RuntimeError):
    """The physical pilot cannot be launched from the supplied local manifest."""


def _fail(message: str) -> NoReturn:
    raise PhysicalPilotDriverError(message)


def _read_bounded_file(path: Path, *, max_bytes: int, name: str) -> bytes:
    try:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except OSError as exc:
        raise PhysicalPilotDriverError(f"{name} could not be read") from exc
    if not payload or len(payload) > max_bytes:
        _fail(f"{name} size is invalid")
    return payload


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _require_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(f"{name} must be non-empty canonical text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PhysicalPilotDriverError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_TEXT_BYTES:
        _fail(f"{name} exceeds the configured byte limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(f"{name} must not contain control characters")
    return value


def _require_logical_artifact_ref(value: object, *, name: str) -> str:
    text = _require_text(value, name=name)
    lowered = text.casefold()
    if (
        text.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\\\/]", text) is not None
        or lowered.startswith("file:")
    ):
        _fail(f"{name} must be a public logical artifact reference, not a local path")
    return text


def _require_sha256(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _require_absolute_path(value: object, *, name: str) -> Path:
    text = _require_text(value, name=name)
    path = Path(text)
    if not path.is_absolute():
        _fail(f"{name} must be an absolute path")
    return path


def _require_percent(value: object, *, name: str) -> float:
    if type(value) not in (int, float):
        _fail(f"{name} must be a finite number in the range (0, 100]")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 < numeric <= 100:
        _fail(f"{name} must be a finite number in the range (0, 100]")
    return numeric


def _require_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _require_float(
    value: object,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if type(value) not in (int, float):
        _fail(f"{name} must be a finite number from {minimum} through {maximum}")
    numeric = float(value)
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        _fail(f"{name} must be a finite number from {minimum} through {maximum}")
    return numeric


@dataclass(frozen=True, slots=True)
class CandidateDescriptorConfig:
    model_id: str
    source_reference: str
    license_reference: str

    @classmethod
    def from_value(cls, value: object) -> CandidateDescriptorConfig:
        if type(value) is not dict or frozenset(value) != _DESCRIPTOR_KEYS:
            _fail("candidate_descriptor fields are invalid")
        config = cls(
            model_id=_require_text(value["model_id"], name="candidate_descriptor.model_id"),
            source_reference=_require_text(
                value["source_reference"],
                name="candidate_descriptor.source_reference",
            ),
            license_reference=_require_text(
                value["license_reference"],
                name="candidate_descriptor.license_reference",
            ),
        )
        try:
            ModelArtifactDescriptor(
                kind=ModelArtifactKind.EXTERNAL_LOCAL,
                provider_id="training-runtime",
                model_id=config.model_id,
                source_reference=config.source_reference,
                license_reference=config.license_reference,
                integrity_basis=ModelIntegrityBasis.SHA256,
                sha256="0" * 64,
                size_bytes=1,
                capabilities=("text",),
            )
        except (TypeError, ValueError) as exc:
            raise PhysicalPilotDriverError(
                "candidate_descriptor provenance is invalid"
            ) from exc
        return config


@dataclass(frozen=True, slots=True)
class ResourceBudgetConfig:
    max_cpu_percent: float
    max_memory_percent: float

    @classmethod
    def from_value(cls, value: object) -> ResourceBudgetConfig:
        if type(value) is not dict or frozenset(value) != _RESOURCE_KEYS:
            _fail("resource_budget fields are invalid")
        return cls(
            max_cpu_percent=_require_percent(
                value["max_cpu_percent"],
                name="resource_budget.max_cpu_percent",
            ),
            max_memory_percent=_require_percent(
                value["max_memory_percent"],
                name="resource_budget.max_memory_percent",
            ),
        )


@dataclass(frozen=True, slots=True)
class TrainerParameters:
    max_sequence_length: int
    learning_rate: float
    lora_r: int
    lora_alpha: int
    lora_dropout: float
    lora_target_modules: tuple[str, ...]
    torch_num_threads: int
    seed: int

    @classmethod
    def from_value(cls, value: object) -> TrainerParameters:
        if type(value) is not dict or frozenset(value) != _TRAINER_KEYS:
            _fail("trainer_parameters fields are invalid")
        raw_targets = value["lora_target_modules"]
        if (
            type(raw_targets) is not list
            or not raw_targets
            or len(raw_targets) > 64
            or any(type(item) is not str for item in raw_targets)
        ):
            _fail("trainer_parameters.lora_target_modules is invalid")
        targets = tuple(
            _require_text(item, name="trainer_parameters.lora_target_modules")
            for item in raw_targets
        )
        if len(set(targets)) != len(targets):
            _fail("trainer_parameters.lora_target_modules contains duplicates")
        if any(_TARGET_MODULE_RE.fullmatch(item) is None for item in targets):
            _fail(
                "trainer_parameters.lora_target_modules must match "
                "the canonical trainer token grammar"
            )
        return cls(
            max_sequence_length=_require_int(
                value["max_sequence_length"],
                name="trainer_parameters.max_sequence_length",
                minimum=32,
                maximum=8192,
            ),
            learning_rate=_require_float(
                value["learning_rate"],
                name="trainer_parameters.learning_rate",
                minimum=1e-8,
                maximum=1.0,
            ),
            lora_r=_require_int(
                value["lora_r"],
                name="trainer_parameters.lora_r",
                minimum=1,
                maximum=1024,
            ),
            lora_alpha=_require_int(
                value["lora_alpha"],
                name="trainer_parameters.lora_alpha",
                minimum=1,
                maximum=65536,
            ),
            lora_dropout=_require_float(
                value["lora_dropout"],
                name="trainer_parameters.lora_dropout",
                minimum=0.0,
                maximum=1.0,
            ),
            lora_target_modules=targets,
            torch_num_threads=_require_int(
                value["torch_num_threads"],
                name="trainer_parameters.torch_num_threads",
                minimum=1,
                maximum=256,
            ),
            seed=_require_int(
                value["seed"],
                name="trainer_parameters.seed",
                minimum=0,
                maximum=(1 << 31) - 1,
            ),
        )


@dataclass(frozen=True, slots=True)
class ScalePlanConfig:
    plan_id: str
    tiers: tuple[TrainingScaleTier, ...]

    @classmethod
    def from_value(cls, value: object) -> ScalePlanConfig:
        if type(value) is not dict or frozenset(value) != _SCALE_PLAN_KEYS:
            _fail("scale_plan fields are invalid")
        raw_tiers = value["tiers"]
        if type(raw_tiers) is not list:
            _fail("scale_plan.tiers must be a bounded list")
        tiers: list[TrainingScaleTier] = []
        for index, raw_tier in enumerate(raw_tiers):
            if type(raw_tier) is not dict or frozenset(raw_tier) != _SCALE_TIER_KEYS:
                _fail(f"scale_plan.tiers[{index}] fields are invalid")
            try:
                tier = TrainingScaleTier(
                    tier_id=_require_text(
                        raw_tier["tier_id"],
                        name=f"scale_plan.tiers[{index}].tier_id",
                    ),
                    max_training_records=_require_int(
                        raw_tier["max_training_records"],
                        name=f"scale_plan.tiers[{index}].max_training_records",
                        minimum=1,
                        maximum=_MAX_SCALE_VALUE,
                    ),
                    max_training_bytes=_require_int(
                        raw_tier["max_training_bytes"],
                        name=f"scale_plan.tiers[{index}].max_training_bytes",
                        minimum=1,
                        maximum=_MAX_SCALE_VALUE,
                    ),
                    max_validation_records=_require_int(
                        raw_tier["max_validation_records"],
                        name=f"scale_plan.tiers[{index}].max_validation_records",
                        minimum=1,
                        maximum=_MAX_SCALE_VALUE,
                    ),
                    max_validation_bytes=_require_int(
                        raw_tier["max_validation_bytes"],
                        name=f"scale_plan.tiers[{index}].max_validation_bytes",
                        minimum=1,
                        maximum=_MAX_SCALE_VALUE,
                    ),
                    max_steps=_require_int(
                        raw_tier["max_steps"],
                        name=f"scale_plan.tiers[{index}].max_steps",
                        minimum=1,
                        maximum=_MAX_SCALE_VALUE,
                    ),
                )
            except TrainingScaleError as exc:
                raise PhysicalPilotDriverError(
                    f"scale_plan.tiers[{index}] is invalid"
                ) from exc
            tiers.append(tier)
        try:
            canonical = TrainingScalePlan(
                plan_id=_require_text(value["plan_id"], name="scale_plan.plan_id"),
                evaluation_set_sha256="0" * 64,
                tiers=tuple(tiers),
            )
        except TrainingScaleError as exc:
            raise PhysicalPilotDriverError("scale_plan is invalid") from exc
        return cls(plan_id=canonical.plan_id, tiers=canonical.tiers)


@dataclass(frozen=True, slots=True)
class PhysicalPilotConfig:
    workspace_id: str
    project_id: str
    owner_id: str
    job_id: str
    blob_store_root: Path
    frozen_package_path: Path
    frozen_package_sha256: str
    trainer_executable: Path
    base_artifact_ref: str
    base_gguf_path: Path
    model_dir: Path
    output_root: Path
    candidate_artifact_ref: str
    candidate_descriptor: CandidateDescriptorConfig
    runtime_versions: tuple[tuple[str, str], ...]
    resource_budget: ResourceBudgetConfig
    trainer_parameters: TrainerParameters
    scale_plan: ScalePlanConfig | None = None

    @classmethod
    def from_json(cls, raw: str | bytes) -> PhysicalPilotConfig:
        if type(raw) not in (str, bytes):
            _fail("physical pilot config must be exact UTF-8 text or bytes")
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        if not encoded or len(encoded) > _CONFIG_MAX_BYTES:
            _fail("physical pilot config size is invalid")
        try:
            decoded = encoded.decode("utf-8")
            value = json.loads(
                decoded,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise PhysicalPilotDriverError("physical pilot config is invalid JSON") from exc
        if type(value) is not dict:
            _fail("physical pilot config fields are invalid")
        schema_version = value.get("schema_version")
        if schema_version == _LEGACY_CONFIG_SCHEMA_VERSION:
            expected_keys = _TOP_LEVEL_KEYS_V1
            scale_plan = None
        elif schema_version == _CONFIG_SCHEMA_VERSION:
            expected_keys = _TOP_LEVEL_KEYS_V2
            if frozenset(value) != expected_keys:
                _fail("physical pilot config fields are invalid")
            scale_plan = ScalePlanConfig.from_value(value["scale_plan"])
        else:
            _fail("unsupported physical pilot config schema")
        if frozenset(value) != expected_keys:
            _fail("physical pilot config fields are invalid")

        raw_versions = value["runtime_versions"]
        if type(raw_versions) is not dict or frozenset(raw_versions) != _RUNTIME_VERSION_KEYS:
            _fail("runtime_versions must declare exactly the canonical training distributions")
        runtime_versions = tuple(
            (
                key,
                _require_text(raw_versions[key], name=f"runtime_versions.{key}"),
            )
            for key in sorted(_RUNTIME_VERSION_KEYS)
        )
        config = cls(
            workspace_id=_require_text(value["workspace_id"], name="workspace_id"),
            project_id=_require_text(value["project_id"], name="project_id"),
            owner_id=_require_text(value["owner_id"], name="owner_id"),
            job_id=_require_text(value["job_id"], name="job_id"),
            blob_store_root=_require_absolute_path(
                value["blob_store_root"], name="blob_store_root"
            ),
            frozen_package_path=_require_absolute_path(
                value["frozen_package_path"], name="frozen_package_path"
            ),
            frozen_package_sha256=_require_sha256(
                value["frozen_package_sha256"], name="frozen_package_sha256"
            ),
            trainer_executable=_require_absolute_path(
                value["trainer_executable"], name="trainer_executable"
            ),
            base_artifact_ref=_require_logical_artifact_ref(
                value["base_artifact_ref"], name="base_artifact_ref"
            ),
            base_gguf_path=_require_absolute_path(
                value["base_gguf_path"], name="base_gguf_path"
            ),
            model_dir=_require_absolute_path(value["model_dir"], name="model_dir"),
            output_root=_require_absolute_path(value["output_root"], name="output_root"),
            candidate_artifact_ref=_require_logical_artifact_ref(
                value["candidate_artifact_ref"], name="candidate_artifact_ref"
            ),
            candidate_descriptor=CandidateDescriptorConfig.from_value(
                value["candidate_descriptor"]
            ),
            runtime_versions=runtime_versions,
            resource_budget=ResourceBudgetConfig.from_value(value["resource_budget"]),
            trainer_parameters=TrainerParameters.from_value(value["trainer_parameters"]),
            scale_plan=scale_plan,
        )
        try:
            TrainingJobSpec(
                job_id=config.job_id,
                task_id="physical-pilot-preflight",
                project_id=config.project_id,
                owner_id=config.owner_id,
                base_artifact=ArtifactIdentity(config.base_artifact_ref, "0" * 64),
                frozen_package_sha256=config.frozen_package_sha256,
                training_material_sha256="0" * 64,
                scale_authorization_sha256="0" * 64,
                candidate_artifact_ref=config.candidate_artifact_ref,
                max_steps=2,
            )
        except (TypeError, ValueError) as exc:
            raise PhysicalPilotDriverError(
                "physical pilot identifiers are incompatible with TrainingJobSpec"
            ) from exc
        return config


def _is_windows() -> bool:
    return os.name == "nt"


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _require_windows_pe_executable(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            dos_header = handle.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                _fail("trainer_executable is not a valid Windows PE executable")
            pe_offset = int.from_bytes(dos_header[60:64], "little")
            if not 64 <= pe_offset <= 16 * 1024 * 1024:
                _fail("trainer_executable has an invalid Windows PE header offset")
            handle.seek(pe_offset)
            signature = handle.read(4)
    except OSError as exc:
        raise PhysicalPilotDriverError(
            "trainer_executable Windows PE header could not be read"
        ) from exc
    if signature != b"PE\0\0":
        _fail("trainer_executable is not a valid Windows PE executable")


def _require_existing_file(path: Path, *, name: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        value = os.lstat(resolved)
    except OSError as exc:
        raise PhysicalPilotDriverError(f"{name} is unavailable") from exc
    if (
        resolved != path
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
        or not stat.S_ISREG(value.st_mode)
    ):
        _fail(f"{name} must be a canonical non-linked file")
    return resolved


def _require_existing_directory(path: Path, *, name: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        value = os.lstat(resolved)
    except OSError as exc:
        raise PhysicalPilotDriverError(f"{name} is unavailable") from exc
    if (
        resolved != path
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
        or not stat.S_ISDIR(value.st_mode)
    ):
        _fail(f"{name} must be a canonical non-linked directory")
    return resolved


def _preflight_output_root(path: Path) -> Path:
    try:
        parent = path.parent.resolve(strict=True)
        parent_stat = os.lstat(parent)
    except OSError as exc:
        raise PhysicalPilotDriverError("output_root parent is unavailable") from exc
    if (
        parent != path.parent
        or stat.S_ISLNK(parent_stat.st_mode)
        or _is_reparse(parent_stat)
        or not stat.S_ISDIR(parent_stat.st_mode)
    ):
        _fail("output_root parent must be canonical and non-linked")
    if path.exists():
        _fail("output_root must not already exist")
    return path


def _require_disjoint_output_root(path: Path, *, protected_roots: tuple[Path, ...]) -> None:
    for root in protected_roots:
        if path == root or path.is_relative_to(root):
            _fail("output_root must not be inside an input authority directory")


def _create_output_root(path: Path) -> Path:
    _preflight_output_root(path)
    try:
        path.mkdir()
        value = os.lstat(path)
    except OSError as exc:
        raise PhysicalPilotDriverError("output_root could not be created") from exc
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        _fail("output_root must be a non-linked directory")
    return path


def _material_totals(materials: ResolvedTrainingPackage) -> tuple[int, int, int, int, int]:
    training_records = 0
    training_bytes = 0
    validation_records = 0
    validation_bytes = 0
    total_records = 0
    for item in materials.evidence.materials:
        total_records += item.record_count
        if item.split is LearningDataSplit.TRAINING:
            training_records += item.record_count
            training_bytes += item.byte_count
        elif item.split is LearningDataSplit.VALIDATION:
            validation_records += item.record_count
            validation_bytes += item.byte_count
    if training_records < 1 or validation_records < 1 or total_records < 2:
        _fail("physical pilot requires non-empty training and validation material")
    if total_records > _TRAINER_MAX_RECORDS:
        _fail("physical pilot material exceeds the canonical PEFT record limit")
    return (
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
        total_records,
    )


def _resource_manager(
    *,
    store: SQLiteStore,
    owner_id: str,
    config: ResourceBudgetConfig,
) -> ResourceManager:
    try:
        from nika_core.resources import PsutilResourceObserver
    except ImportError as exc:
        raise PhysicalPilotDriverError(
            "physical pilot resource observer dependency is unavailable"
        ) from exc
    resources = ResourceManager(store, PsutilResourceObserver())
    resources.set_budget(
        ResourceBudget(
            scope="model_training",
            owner_id=owner_id,
            max_concurrent=1,
            max_cpu_percent=config.max_cpu_percent,
            max_memory_percent=config.max_memory_percent,
        )
    )
    return resources


def _build_worker(
    *,
    store: SQLiteStore,
    trainer_executable: Path,
    trainer_artifact_id: str,
    local_root: Path,
    base_gguf_path: Path,
    model_dir: Path,
    output_root: Path,
    parameters: TrainerParameters,
    max_records: int,
) -> tuple[SubprocessTrainingWorker, ArtifactRecord]:
    registry = ArtifactRegistry.from_store(store, local_file_roots=(local_root,))
    trainer_record = registry.get(trainer_artifact_id)
    environment = build_trainer_environment(
        base_gguf=base_gguf_path,
        model_dir=model_dir,
        output_root=output_root,
        max_records=max_records,
        max_sequence_length=parameters.max_sequence_length,
        learning_rate=parameters.learning_rate,
        lora_r=parameters.lora_r,
        lora_alpha=parameters.lora_alpha,
        lora_dropout=parameters.lora_dropout,
        lora_target_modules=parameters.lora_target_modules,
        trainer_artifact=trainer_record,
        torch_num_threads=parameters.torch_num_threads,
        seed=parameters.seed,
    )
    return (
        SubprocessTrainingWorker(
            (os.fspath(trainer_executable),),
            artifact_registry=registry,
            trainer_artifact_id=trainer_record.artifact_id,
            environment=environment,
        ),
        trainer_record,
    )


def _candidate_descriptor(
    *,
    config: PhysicalPilotConfig,
    candidate_path: Path,
    completed: TrainingRunEvidence,
) -> ModelArtifactDescriptor:
    if completed.candidate_sha256 is None:
        _fail("completed physical pilot is missing candidate digest")
    try:
        size_bytes = candidate_path.stat().st_size
    except OSError as exc:
        raise PhysicalPilotDriverError("candidate artifact size could not be read") from exc
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="training-runtime",
        model_id=config.candidate_descriptor.model_id,
        model_version=completed.candidate_sha256,
        source_reference=config.candidate_descriptor.source_reference,
        license_reference=config.candidate_descriptor.license_reference,
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=completed.candidate_sha256,
        size_bytes=size_bytes,
        capabilities=("text",),
    )


def _scale_plan_for_physical_pilot(
    config: PhysicalPilotConfig,
    *,
    evaluation_set_sha256: str,
    training_records: int,
    training_bytes: int,
    validation_records: int,
    validation_bytes: int,
) -> TrainingScalePlan:
    if config.scale_plan is None:
        return TrainingScalePlan(
            plan_id="physical-pilot",
            evaluation_set_sha256=evaluation_set_sha256,
            tiers=(
                TrainingScaleTier(
                    tier_id="pilot",
                    max_training_records=training_records,
                    max_training_bytes=training_bytes,
                    max_validation_records=validation_records,
                    max_validation_bytes=validation_bytes,
                    max_steps=2,
                ),
            ),
        )
    try:
        return TrainingScalePlan(
            plan_id=config.scale_plan.plan_id,
            evaluation_set_sha256=evaluation_set_sha256,
            tiers=config.scale_plan.tiers,
        )
    except TrainingScaleError as exc:
        raise PhysicalPilotDriverError(
            "configured training scale plan is invalid for the frozen package"
        ) from exc


def run_physical_pilot_from_config(
    config: PhysicalPilotConfig,
) -> PhysicalTrainingPilotReport:
    if type(config) is not PhysicalPilotConfig:
        raise TypeError("config must be exact PhysicalPilotConfig")
    if not _is_windows():
        _fail("physical PEFT pilot driver must execute on Windows")

    blob_store_root = _require_existing_directory(
        config.blob_store_root,
        name="blob_store_root",
    )
    frozen_package_path = _require_existing_file(
        config.frozen_package_path,
        name="frozen_package_path",
    )
    trainer_executable = _require_existing_file(
        config.trainer_executable,
        name="trainer_executable",
    )
    if trainer_executable.suffix.casefold() != ".exe":
        _fail("trainer_executable must be a Windows executable")
    _require_windows_pe_executable(trainer_executable)
    base_gguf_path = _require_existing_file(config.base_gguf_path, name="base_gguf_path")
    if base_gguf_path.suffix.casefold() != ".gguf":
        _fail("base_gguf_path must use the .gguf suffix")
    model_dir = _require_existing_directory(config.model_dir, name="model_dir")
    try:
        model_directory_manifest_sha256(model_dir)
    except ValueError as exc:
        raise PhysicalPilotDriverError(
            "model_dir is not a canonical local model directory"
        ) from exc
    output_root = _preflight_output_root(config.output_root)
    _require_disjoint_output_root(
        output_root,
        protected_roots=(blob_store_root, model_dir),
    )

    package_bytes = _read_bounded_file(
        frozen_package_path,
        max_bytes=_FROZEN_PACKAGE_MAX_BYTES,
        name="frozen learning package",
    )
    package = FrozenLearningPackage.from_json(
        package_bytes,
        expected_manifest_sha256=config.frozen_package_sha256,
    )
    blob_store = ContentAddressedBlobStore(blob_store_root)
    materials = resolve_training_materials(
        package,
        workspace_id=config.workspace_id,
        blob_store=blob_store,
    )
    (
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
        total_records,
    ) = _material_totals(materials)
    runtime_metadata = build_training_runtime_metadata(dict(config.runtime_versions))

    output_root = _create_output_root(output_root)
    database_path = output_root / "physical-pilot.sqlite3"
    report_path = output_root / "physical-pilot-report.json"
    store = SQLiteStore(database_path)
    store.initialize()
    registry = ArtifactRegistry.from_store(
        store,
        local_file_roots=(trainer_executable.parent,),
    )
    trainer_record = registry.register_file(
        workspace_id=config.workspace_id,
        idempotency_key="physical-peft-trainer",
        path=trainer_executable,
        kind="training_executable",
        metadata=runtime_metadata,
    )
    initial_worker, trainer_record = _build_worker(
        store=store,
        trainer_executable=trainer_executable,
        trainer_artifact_id=trainer_record.artifact_id,
        local_root=trainer_executable.parent,
        base_gguf_path=base_gguf_path,
        model_dir=model_dir,
        output_root=output_root,
        parameters=config.trainer_parameters,
        max_records=total_records,
    )

    base_artifact = ArtifactIdentity(
        config.base_artifact_ref,
        materials.evidence.base_artifact_sha256,
    )
    scale_plan = _scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256=materials.evidence.evaluation_set_sha256,
        training_records=training_records,
        training_bytes=training_bytes,
        validation_records=validation_records,
        validation_bytes=validation_bytes,
    )
    pilot_tier = scale_plan.tiers[0]
    try:
        scale_authorization = authorize_training_scale(
            plan=scale_plan,
            tier_id=pilot_tier.tier_id,
            job_id=config.job_id,
            base_artifact=base_artifact,
            candidate_artifact_ref=config.candidate_artifact_ref,
            material_evidence=materials.evidence,
            execution_plan_sha256=initial_worker.execution_plan_sha256,
            max_steps=2,
        )
    except TrainingScaleError as exc:
        raise PhysicalPilotDriverError(
            "pilot training data exceeds the configured first scale tier"
        ) from exc
    task = TaskQueue(store).create(
        workspace_id=config.workspace_id,
        agent_id="physical-peft-pilot",
        payload={
            "job_id": config.job_id,
            "kind": "physical_peft_pilot",
            "scale_plan_sha256": scale_plan.plan_sha256,
            "scale_tier_id": pilot_tier.tier_id,
        },
    )
    spec = TrainingJobSpec(
        job_id=config.job_id,
        task_id=task.task_id,
        project_id=config.project_id,
        owner_id=config.owner_id,
        base_artifact=base_artifact,
        frozen_package_sha256=materials.evidence.package_manifest_sha256,
        training_material_sha256=materials.training_material_sha256,
        scale_authorization_sha256=scale_authorization.authorization_sha256,
        candidate_artifact_ref=config.candidate_artifact_ref,
        max_steps=2,
    )
    resources = _resource_manager(
        store=store,
        owner_id=config.owner_id,
        config=config.resource_budget,
    )
    runtime = TrainingRuntime(
        resources=resources,
        checkpoints=CheckpointService(store),
        training_materials=materials,
    )

    def restart_runtime() -> TrainingRuntime:
        restart_store = SQLiteStore(database_path)
        restart_store.initialize()
        restart_materials = resolve_training_materials(
            package,
            workspace_id=config.workspace_id,
            blob_store=ContentAddressedBlobStore(blob_store_root),
        )
        return TrainingRuntime(
            resources=_resource_manager(
                store=restart_store,
                owner_id=config.owner_id,
                config=config.resource_budget,
            ),
            checkpoints=CheckpointService(restart_store),
            training_materials=restart_materials,
        )

    def restart_worker() -> SubprocessTrainingWorker:
        restart_store = SQLiteStore(database_path)
        restart_worker_value, _ = _build_worker(
            store=restart_store,
            trainer_executable=trainer_executable,
            trainer_artifact_id=trainer_record.artifact_id,
            local_root=trainer_executable.parent,
            base_gguf_path=base_gguf_path,
            model_dir=model_dir,
            output_root=output_root,
            parameters=config.trainer_parameters,
            max_records=total_records,
        )
        return restart_worker_value

    candidate_path = candidate_artifact_path(
        output_root,
        config.candidate_artifact_ref,
    )
    report = run_physical_training_pilot(
        runtime=runtime,
        restart_runtime=restart_runtime,
        spec=spec,
        worker=initial_worker,
        restart_worker=restart_worker,
        scale_authorization=scale_authorization,
        candidate_path=candidate_path,
        candidate_descriptor_factory=lambda completed: _candidate_descriptor(
            config=config,
            candidate_path=candidate_path,
            completed=completed,
        ),
        candidate_root=output_root,
    )
    write_physical_training_pilot_report(report, report_path)
    _LOG.info(
        "physical PEFT pilot completed: job_id=%s candidate_sha256=%s",
        report.job_id,
        report.candidate_sha256,
    )
    return report


def _read_config(path: Path) -> PhysicalPilotConfig:
    raw = _read_bounded_file(
        path,
        max_bytes=_CONFIG_MAX_BYTES,
        name="physical pilot config",
    )
    return PhysicalPilotConfig.from_json(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the canonical Windows physical PEFT pause/reopen/resume pilot."
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to the local UTF-8 physical-pilot JSON manifest.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config_path = args.config
        if not config_path.is_absolute():
            config_path = config_path.resolve(strict=True)
        config = _read_config(config_path)
        report = run_physical_pilot_from_config(config)
    except (
        KeyError,
        OSError,
        PhysicalPilotDriverError,
        PhysicalTrainingPilotError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        _LOG.error("physical PEFT pilot failed: %s", exc)
        return 1
    print(report.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
