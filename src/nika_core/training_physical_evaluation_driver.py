from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ExperimentEngine,
    ExperimentStatus,
    MetricRule,
    PromotionPolicy,
    SQLiteExperimentRepository,
)
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.learning_package import FrozenLearningPackage
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import (
    BenchmarkExecutionConfig,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_engineering.experiment_bridge import build_experiment_definition
from nika_core.model_gateway.contracts import ModelMessage, PrivacyClass, ProviderKind
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)
from nika_core.training_evaluation_binding import bind_training_result_for_evaluation
from nika_core.training_evaluation_champion import bind_champion_for_attested_evaluation
from nika_core.training_evaluation_champion_execution import run_attested_champion_benchmark
from nika_core.training_evaluation_comparison import (
    AttestedTrainingComparisonResult,
    run_attested_old_vs_new_comparison,
)
from nika_core.training_evaluation_execution import run_attested_challenger_benchmark
from nika_core.training_evaluation_subprocess import RegistrySubprocessLoadedModelAttestor
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
    TrainingStatusService,
)

_LOG = logging.getLogger(__name__)
_CONFIG_SCHEMA_VERSION = 1
_REPORT_SCHEMA_VERSION = 1
_CONFIG_MAX_BYTES = 128 * 1024
_EVALUATION_SET_MAX_BYTES = 8 * 1024 * 1024
_FROZEN_PACKAGE_MAX_BYTES = 16 * 1024 * 1024
_PILOT_REPORT_MAX_BYTES = 64 * 1024
_REPORT_MAX_BYTES = 64 * 1024
_MAX_TEXT_BYTES = 4096
_MAX_EVALUATION_TEXT_BYTES = 1024 * 1024
_MAX_CASES = 10_000
_MAX_MESSAGES_PER_CASE = 128
_MAX_COMMAND_FILES = 16
_MAX_SWITCHES = 16
_SWITCH_RE = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "schema",
        "physical_pilot_evidence_sha256",
        "requested_experiment_id",
        "evaluation_set_sha256",
        "execution_config_sha256",
        "comparison_evidence_sha256",
        "experiment_id",
        "experiment_status",
        "selected_candidate_id",
        "previous_champion_id",
        "training_binding_sha256",
        "champion_benchmark_sha256",
        "challenger_benchmark_sha256",
        "attestor_id",
        "attestor_sha256",
        "champion_provider_manifest_sha256",
        "challenger_provider_manifest_sha256",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "project_id",
        "owner_id",
        "physical_pilot_output_root",
        "frozen_package_path",
        "base_artifact_ref",
        "base_model_path",
        "candidate_model_path",
        "base_model",
        "candidate_model",
        "evaluator",
        "evaluation_set_path",
        "experiment_id",
        "permission_fingerprint",
        "benchmark",
        "policy",
    }
)
_BASE_MODEL_KEYS = frozenset(
    {
        "provider_id",
        "model_id",
        "model_version",
        "source_reference",
        "license_reference",
        "capabilities",
    }
)
_CANDIDATE_MODEL_KEYS = frozenset(
    {"model_id", "source_reference", "license_reference"}
)
_EVALUATOR_KEYS = frozenset(
    {
        "executable",
        "command_files",
        "switches",
        "provenance_ref",
        "license_ref",
    }
)
_BENCHMARK_KEYS = frozenset({"timeout_seconds", "temperature", "scorer_id"})
_POLICY_KEYS = frozenset(
    {
        "primary_metric",
        "minimum_improvement",
        "minimum_replays",
        "primary_higher_is_better",
        "guardrails",
    }
)
_GUARDRAIL_KEYS = frozenset({"metric", "higher_is_better", "max_regression"})
_EVALUATION_KEYS = frozenset(
    {
        "evaluation_set_id",
        "version",
        "provenance_ref",
        "license_ref",
        "purpose",
        "privacy",
        "cases",
    }
)
_CASE_KEYS = frozenset({"case_id", "messages", "expected_text", "pass_score", "weight"})
_MESSAGE_KEYS = frozenset({"role", "content"})


class PhysicalEvaluationDriverError(RuntimeError):
    """Physical old-vs-new evaluation cannot proceed from the supplied local evidence."""


def _fail(message: str) -> NoReturn:
    raise PhysicalEvaluationDriverError(message)


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
        raise PhysicalEvaluationDriverError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_TEXT_BYTES:
        _fail(f"{name} exceeds the configured byte limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(f"{name} must not contain control characters")
    return value


def _require_evaluation_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        _fail(f"{name} must be non-empty text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PhysicalEvaluationDriverError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_EVALUATION_TEXT_BYTES:
        _fail(f"{name} exceeds the configured byte limit")
    return value


def _require_absolute_path(value: object, *, name: str) -> Path:
    path = Path(_require_text(value, name=name))
    if not path.is_absolute():
        _fail(f"{name} must be an absolute path")
    return path


def _require_number(
    value: object,
    *,
    name: str,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if type(value) not in (int, float):
        _fail(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise PhysicalEvaluationDriverError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum:
        _fail(f"{name} must be a finite number not below {minimum}")
    if maximum is not None and number > maximum:
        _fail(f"{name} must not exceed {maximum}")
    return number


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _canonical_directory(path: Path, *, name: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        snapshot = os.lstat(path)
    except OSError as exc:
        raise PhysicalEvaluationDriverError(f"{name} is unavailable") from exc
    if (
        resolved != path
        or stat.S_ISLNK(snapshot.st_mode)
        or _is_reparse(snapshot)
        or not stat.S_ISDIR(snapshot.st_mode)
    ):
        _fail(f"{name} must be a canonical non-linked directory")
    return resolved


def _canonical_file(path: Path, *, name: str) -> os.stat_result:
    try:
        resolved = path.resolve(strict=True)
        snapshot = os.lstat(path)
    except OSError as exc:
        raise PhysicalEvaluationDriverError(f"{name} is unavailable") from exc
    if (
        resolved != path
        or stat.S_ISLNK(snapshot.st_mode)
        or _is_reparse(snapshot)
        or not stat.S_ISREG(snapshot.st_mode)
    ):
        _fail(f"{name} must be a canonical non-linked regular file")
    if snapshot.st_size < 1:
        _fail(f"{name} must not be empty")
    return snapshot


def _require_windows_pe_executable(path: Path, *, name: str) -> None:
    before = _canonical_file(path, name=name)
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            _fail(f"{name} changed before its PE header was opened")
        dos_header = os.read(descriptor, 64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            _fail(f"{name} is not a valid Windows PE executable")
        pe_offset = int.from_bytes(dos_header[60:64], "little")
        if not 64 <= pe_offset <= 16 * 1024 * 1024:
            _fail(f"{name} has an invalid Windows PE header offset")
        os.lseek(descriptor, pe_offset, os.SEEK_SET)
        signature = os.read(descriptor, 4)
        after = os.fstat(descriptor)
    except PhysicalEvaluationDriverError:
        raise
    except OSError as exc:
        raise PhysicalEvaluationDriverError(
            f"{name} Windows PE header could not be read"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if signature != b"PE\0\0":
        _fail(f"{name} is not a valid Windows PE executable")
    if (
        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or opened.st_size != after.st_size
        or getattr(opened, "st_mtime_ns", None) != getattr(after, "st_mtime_ns", None)
    ):
        _fail(f"{name} changed while its PE header was read")


def _read_regular_file(path: Path, *, name: str, max_bytes: int) -> bytes:
    before = _canonical_file(path, name=name)
    if before.st_size > max_bytes:
        _fail(f"{name} size is outside the admitted range")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            _fail(f"{name} changed before it was opened")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            payload = handle.read(max_bytes + 1)
            after_open = os.fstat(handle.fileno())
        after_path = _canonical_file(path, name=name)
    except PhysicalEvaluationDriverError:
        raise
    except OSError as exc:
        raise PhysicalEvaluationDriverError(f"{name} could not be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > max_bytes:
        _fail(f"{name} exceeds the configured byte limit")
    if (
        (opened.st_dev, opened.st_ino) != (after_open.st_dev, after_open.st_ino)
        or (opened.st_dev, opened.st_ino) != (after_path.st_dev, after_path.st_ino)
        or opened.st_size != after_open.st_size
        or opened.st_size != after_path.st_size
        or getattr(opened, "st_mtime_ns", None) != getattr(after_open, "st_mtime_ns", None)
        or getattr(opened, "st_mtime_ns", None) != getattr(after_path, "st_mtime_ns", None)
    ):
        _fail(f"{name} changed while it was being read")
    return payload


def _read_utf8(path: Path, *, name: str, max_bytes: int) -> str:
    raw = _read_regular_file(path, name=name, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PhysicalEvaluationDriverError(f"{name} must be valid UTF-8") from exc


@dataclass(frozen=True, slots=True)
class BaseModelConfig:
    provider_id: str
    model_id: str
    model_version: str | None
    source_reference: str
    license_reference: str
    capabilities: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object) -> BaseModelConfig:
        if type(value) is not dict or frozenset(value) != _BASE_MODEL_KEYS:
            _fail("base_model fields are invalid")
        raw_capabilities = value["capabilities"]
        if (
            type(raw_capabilities) is not list
            or not raw_capabilities
            or len(raw_capabilities) > 128
            or any(type(item) is not str for item in raw_capabilities)
        ):
            _fail("base_model.capabilities must be a bounded non-empty text list")
        capabilities = tuple(
            _require_text(item, name="base_model.capabilities")
            for item in raw_capabilities
        )
        if len(set(capabilities)) != len(capabilities):
            _fail("base_model.capabilities contains duplicates")
        raw_version = value["model_version"]
        model_version = (
            None
            if raw_version is None
            else _require_text(raw_version, name="base_model.model_version")
        )
        return cls(
            provider_id=_require_text(value["provider_id"], name="base_model.provider_id"),
            model_id=_require_text(value["model_id"], name="base_model.model_id"),
            model_version=model_version,
            source_reference=_require_text(
                value["source_reference"], name="base_model.source_reference"
            ),
            license_reference=_require_text(
                value["license_reference"], name="base_model.license_reference"
            ),
            capabilities=capabilities,
        )


@dataclass(frozen=True, slots=True)
class CandidateModelConfig:
    model_id: str
    source_reference: str
    license_reference: str

    @classmethod
    def from_value(cls, value: object) -> CandidateModelConfig:
        if type(value) is not dict or frozenset(value) != _CANDIDATE_MODEL_KEYS:
            _fail("candidate_model fields are invalid")
        return cls(
            model_id=_require_text(value["model_id"], name="candidate_model.model_id"),
            source_reference=_require_text(
                value["source_reference"], name="candidate_model.source_reference"
            ),
            license_reference=_require_text(
                value["license_reference"], name="candidate_model.license_reference"
            ),
        )


@dataclass(frozen=True, slots=True)
class EvaluatorConfig:
    executable: Path
    command_files: tuple[Path, ...]
    switches: tuple[str, ...]
    provenance_ref: str
    license_ref: str

    @classmethod
    def from_value(cls, value: object) -> EvaluatorConfig:
        if type(value) is not dict or frozenset(value) != _EVALUATOR_KEYS:
            _fail("evaluator fields are invalid")
        raw_files = value["command_files"]
        raw_switches = value["switches"]
        if (
            type(raw_files) is not list
            or len(raw_files) > _MAX_COMMAND_FILES
            or any(type(item) is not str for item in raw_files)
        ):
            _fail("evaluator.command_files must be a bounded path list")
        if (
            type(raw_switches) is not list
            or len(raw_switches) > _MAX_SWITCHES
            or any(type(item) is not str for item in raw_switches)
        ):
            _fail("evaluator.switches must be a bounded text list")
        files = tuple(
            _require_absolute_path(item, name="evaluator.command_files")
            for item in raw_files
        )
        switches = tuple(raw_switches)
        if any(_SWITCH_RE.fullmatch(item) is None for item in switches):
            _fail("evaluator.switches must use simple --option tokens")
        if len(set(switches)) != len(switches):
            _fail("evaluator.switches contains duplicates")
        provenance_ref = _require_text(
            value["provenance_ref"], name="evaluator.provenance_ref"
        )
        license_ref = _require_text(value["license_ref"], name="evaluator.license_ref")
        try:
            ModelArtifactDescriptor(
                kind=ModelArtifactKind.EXTERNAL_LOCAL,
                provider_id="evaluation-runtime",
                model_id="evaluator-authority",
                source_reference=provenance_ref,
                license_reference=license_ref,
                integrity_basis=ModelIntegrityBasis.SHA256,
                sha256="0" * 64,
                size_bytes=1,
            )
        except (TypeError, ValueError) as exc:
            raise PhysicalEvaluationDriverError(
                "evaluator provenance must be public and secret-free"
            ) from exc
        return cls(
            executable=_require_absolute_path(value["executable"], name="evaluator.executable"),
            command_files=files,
            switches=switches,
            provenance_ref=provenance_ref,
            license_ref=license_ref,
        )


def _benchmark_from_value(value: object) -> BenchmarkExecutionConfig:
    if type(value) is not dict or frozenset(value) != _BENCHMARK_KEYS:
        _fail("benchmark fields are invalid")
    timeout = _require_number(
        value["timeout_seconds"],
        name="benchmark.timeout_seconds",
        minimum=0.001,
        maximum=86_400.0,
    )
    raw_temperature = value["temperature"]
    if raw_temperature is None:
        temperature = None
    else:
        temperature = _require_number(
            raw_temperature,
            name="benchmark.temperature",
            minimum=0.0,
            maximum=2.0,
        )
    scorer_id = _require_text(value["scorer_id"], name="benchmark.scorer_id")
    try:
        return BenchmarkExecutionConfig(
            timeout_seconds=timeout,
            temperature=temperature,
            scorer_id=scorer_id,
        )
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("benchmark configuration is invalid") from exc


def _policy_from_value(value: object) -> PromotionPolicy:
    if type(value) is not dict or frozenset(value) != _POLICY_KEYS:
        _fail("policy fields are invalid")
    raw_guardrails = value["guardrails"]
    if (
        type(raw_guardrails) is not list
        or len(raw_guardrails) > 16
        or any(type(item) is not dict for item in raw_guardrails)
    ):
        _fail("policy.guardrails must be a bounded object list")
    guardrails: list[MetricRule] = []
    for item in raw_guardrails:
        if frozenset(item) != _GUARDRAIL_KEYS:
            _fail("policy guardrail fields are invalid")
        if type(item["higher_is_better"]) is not bool:
            _fail("policy.guardrails.higher_is_better must be boolean")
        guardrails.append(
            MetricRule(
                metric=_require_text(item["metric"], name="policy.guardrails.metric"),
                higher_is_better=item["higher_is_better"],
                max_regression=_require_number(
                    item["max_regression"],
                    name="policy.guardrails.max_regression",
                    minimum=0.0,
                ),
            )
        )
    if type(value["minimum_replays"]) is not int:
        _fail("policy.minimum_replays must be an integer")
    if type(value["primary_higher_is_better"]) is not bool:
        _fail("policy.primary_higher_is_better must be boolean")
    try:
        return PromotionPolicy(
            primary_metric=_require_text(value["primary_metric"], name="policy.primary_metric"),
            minimum_improvement=_require_number(
                value["minimum_improvement"],
                name="policy.minimum_improvement",
                minimum=0.0,
            ),
            minimum_replays=value["minimum_replays"],
            guardrails=tuple(guardrails),
            primary_higher_is_better=value["primary_higher_is_better"],
        )
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("promotion policy is invalid") from exc


@dataclass(frozen=True, slots=True)
class PhysicalEvaluationConfig:
    workspace_id: str
    project_id: str
    owner_id: str
    physical_pilot_output_root: Path
    frozen_package_path: Path
    base_artifact_ref: str
    base_model_path: Path
    candidate_model_path: Path
    base_model: BaseModelConfig
    candidate_model: CandidateModelConfig
    evaluator: EvaluatorConfig
    evaluation_set_path: Path
    experiment_id: str
    permission_fingerprint: str
    benchmark: BenchmarkExecutionConfig
    policy: PromotionPolicy

    @classmethod
    def from_json(cls, raw: str | bytes) -> PhysicalEvaluationConfig:
        if type(raw) not in (str, bytes):
            _fail("physical evaluation config must be exact UTF-8 text or bytes")
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        if not encoded or len(encoded) > _CONFIG_MAX_BYTES:
            _fail("physical evaluation config size is invalid")
        try:
            value = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise PhysicalEvaluationDriverError(
                "physical evaluation config is invalid JSON"
            ) from exc
        if type(value) is not dict or frozenset(value) != _TOP_LEVEL_KEYS:
            _fail("physical evaluation config fields are invalid")
        if value["schema_version"] != _CONFIG_SCHEMA_VERSION:
            _fail("unsupported physical evaluation config schema")
        return cls(
            workspace_id=_require_text(value["workspace_id"], name="workspace_id"),
            project_id=_require_text(value["project_id"], name="project_id"),
            owner_id=_require_text(value["owner_id"], name="owner_id"),
            physical_pilot_output_root=_require_absolute_path(
                value["physical_pilot_output_root"],
                name="physical_pilot_output_root",
            ),
            frozen_package_path=_require_absolute_path(
                value["frozen_package_path"], name="frozen_package_path"
            ),
            base_artifact_ref=_require_text(
                value["base_artifact_ref"], name="base_artifact_ref"
            ),
            base_model_path=_require_absolute_path(
                value["base_model_path"], name="base_model_path"
            ),
            candidate_model_path=_require_absolute_path(
                value["candidate_model_path"], name="candidate_model_path"
            ),
            base_model=BaseModelConfig.from_value(value["base_model"]),
            candidate_model=CandidateModelConfig.from_value(value["candidate_model"]),
            evaluator=EvaluatorConfig.from_value(value["evaluator"]),
            evaluation_set_path=_require_absolute_path(
                value["evaluation_set_path"], name="evaluation_set_path"
            ),
            experiment_id=_require_text(value["experiment_id"], name="experiment_id"),
            permission_fingerprint=_require_text(
                value["permission_fingerprint"], name="permission_fingerprint"
            ),
            benchmark=_benchmark_from_value(value["benchmark"]),
            policy=_policy_from_value(value["policy"]),
        )


def _evaluation_set_from_json(raw: str) -> EvaluationSet:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PhysicalEvaluationDriverError("evaluation set is invalid JSON") from exc
    if type(value) is not dict or frozenset(value) != _EVALUATION_KEYS:
        _fail("evaluation set fields are invalid")
    if value["purpose"] != EvaluationPurpose.HELD_OUT.value:
        _fail("physical old-vs-new evaluation requires purpose=held_out")
    try:
        privacy = PrivacyClass(value["privacy"])
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("evaluation set privacy is invalid") from exc
    raw_cases = value["cases"]
    if (
        type(raw_cases) is not list
        or not raw_cases
        or len(raw_cases) > _MAX_CASES
        or any(type(item) is not dict for item in raw_cases)
    ):
        _fail("evaluation set cases are invalid or unbounded")
    cases: list[EvaluationCase] = []
    for raw_case in raw_cases:
        if frozenset(raw_case) != _CASE_KEYS:
            _fail("evaluation case fields are invalid")
        raw_messages = raw_case["messages"]
        if (
            type(raw_messages) is not list
            or not raw_messages
            or len(raw_messages) > _MAX_MESSAGES_PER_CASE
            or any(type(item) is not dict for item in raw_messages)
        ):
            _fail("evaluation case messages are invalid or unbounded")
        messages: list[ModelMessage] = []
        for raw_message in raw_messages:
            if frozenset(raw_message) != _MESSAGE_KEYS:
                _fail("evaluation message fields are invalid")
            try:
                messages.append(
                    ModelMessage(
                        role=_require_text(raw_message["role"], name="evaluation message role"),
                        content=_require_evaluation_text(
                            raw_message["content"], name="evaluation message content"
                        ),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise PhysicalEvaluationDriverError(
                    "evaluation message is invalid"
                ) from exc
        try:
            cases.append(
                EvaluationCase(
                    case_id=_require_text(raw_case["case_id"], name="evaluation case_id"),
                    messages=tuple(messages),
                    expected_text=_require_evaluation_text(
                        raw_case["expected_text"], name="evaluation expected_text"
                    ),
                    pass_score=raw_case["pass_score"],
                    weight=raw_case["weight"],
                )
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise PhysicalEvaluationDriverError("evaluation case is invalid") from exc
    try:
        return EvaluationSet(
            evaluation_set_id=_require_text(
                value["evaluation_set_id"], name="evaluation_set_id"
            ),
            version=_require_text(value["version"], name="evaluation_set version"),
            provenance_ref=_require_text(
                value["provenance_ref"], name="evaluation_set provenance_ref"
            ),
            license_ref=_require_text(
                value["license_ref"], name="evaluation_set license_ref"
            ),
            purpose=EvaluationPurpose.HELD_OUT,
            privacy=privacy,
            cases=tuple(cases),
        )
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("evaluation set is invalid") from exc


def _physical_report(output_root: Path) -> PhysicalTrainingPilotReport:
    raw = _read_utf8(
        output_root / "physical-pilot-report.json",
        name="physical pilot report",
        max_bytes=_PILOT_REPORT_MAX_BYTES,
    )
    try:
        return PhysicalTrainingPilotReport.from_json(raw.rstrip("\n"))
    except (RuntimeError, TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError(
            "physical pilot report is not canonical"
        ) from exc


def _find_pilot_task(
    store: SQLiteStore,
    *,
    workspace_id: str,
    job_id: str,
) -> TaskRecord:
    expected_payload = {"job_id": job_id, "kind": "physical_peft_pilot"}
    matches = tuple(
        task
        for task in TaskQueue(store).list_recent(limit=500)
        if task.workspace_id == workspace_id
        and task.agent_id == "physical-peft-pilot"
        and task.payload == expected_payload
    )
    if len(matches) != 1:
        _fail("physical pilot database must contain exactly one matching training task")
    return matches[0]


def _verify_completed_checkpoint(
    store: SQLiteStore,
    *,
    task: TaskRecord,
    report: PhysicalTrainingPilotReport,
) -> None:
    checkpoints = CheckpointService(store)
    status = TrainingStatusService(checkpoints).read(task.task_id)
    if status is None:
        _fail("physical pilot completion checkpoint is missing")
    if (
        status.state is not TrainingRunState.COMPLETED
        or status.checkpoint_id != report.completed_checkpoint_id
        or status.next_step != report.completed_steps
        or status.reason is not None
    ):
        _fail("physical pilot latest durable status does not match the completion report")
    checkpoint = checkpoints.latest(task.task_id)
    if checkpoint is None or checkpoint.checkpoint_id != status.checkpoint_id:
        _fail("physical pilot checkpoint changed during verification")
    payload = checkpoint.payload
    expected = {
        "job_id": report.job_id,
        "job_fingerprint": report.job_fingerprint,
        "frozen_package_sha256": report.frozen_package_sha256,
        "training_material_sha256": report.training_material_sha256,
        "scale_authorization_sha256": report.scale_authorization_sha256,
        "next_step": report.completed_steps,
        "candidate_artifact_ref": report.candidate_artifact_ref,
        "candidate_sha256": report.candidate_sha256,
        "reason": None,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        _fail("physical pilot checkpoint payload does not match the completion report")


def _model_size(path: Path, *, name: str) -> int:
    return int(_canonical_file(path, name=name).st_size)


def _base_descriptor(
    config: BaseModelConfig,
    *,
    report: PhysicalTrainingPilotReport,
    size_bytes: int,
) -> ModelArtifactDescriptor:
    try:
        return ModelArtifactDescriptor(
            kind=ModelArtifactKind.EXTERNAL_LOCAL,
            provider_id=config.provider_id,
            model_id=config.model_id,
            model_version=config.model_version,
            source_reference=config.source_reference,
            license_reference=config.license_reference,
            integrity_basis=ModelIntegrityBasis.SHA256,
            sha256=report.base_sha256,
            size_bytes=size_bytes,
            capabilities=config.capabilities,
        )
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("base model descriptor is invalid") from exc


def _candidate_descriptor(
    config: CandidateModelConfig,
    *,
    report: PhysicalTrainingPilotReport,
) -> ModelArtifactDescriptor:
    try:
        descriptor = ModelArtifactDescriptor(
            kind=ModelArtifactKind.EXTERNAL_LOCAL,
            provider_id="training-runtime",
            model_id=config.model_id,
            model_version=report.candidate_sha256,
            source_reference=config.source_reference,
            license_reference=config.license_reference,
            integrity_basis=ModelIntegrityBasis.SHA256,
            sha256=report.candidate_sha256,
            size_bytes=report.candidate_byte_count,
            capabilities=("text",),
        )
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("candidate model descriptor is invalid") from exc
    if (
        descriptor.descriptor_digest != report.candidate_descriptor_sha256
        or descriptor.registry_key != report.candidate_registry_key
    ):
        _fail("candidate descriptor metadata does not match physical pilot evidence")
    return descriptor


def _candidate(
    *,
    candidate_id: str,
    descriptor: ModelArtifactDescriptor,
    evaluator: EvaluatorConfig,
) -> ModelCandidate:
    if descriptor.sha256 is None:
        _fail("physical model descriptor is missing SHA-256 integrity")
    try:
        return ModelCandidate(
            candidate_id=candidate_id,
            provider_id=descriptor.provider_id,
            provider_kind=ProviderKind.LOCAL,
            request_model=descriptor.model_id,
            expected_response_model=descriptor.model_id,
            engine_provenance_ref=evaluator.provenance_ref,
            engine_license_ref=evaluator.license_ref,
            model_provenance_ref=descriptor.source_reference,
            model_license_ref=descriptor.license_reference,
            model_sha256=descriptor.sha256,
        )
    except (TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError("physical model candidate is invalid") from exc


def _register_evaluator(
    *,
    store: SQLiteStore,
    workspace_id: str,
    evaluator: EvaluatorConfig,
) -> tuple[ArtifactRegistry, tuple[str, ...], str, dict[int, str]]:
    executable = Path(evaluator.executable)
    _canonical_file(executable, name="evaluator executable")
    if executable.suffix.casefold() != ".exe":
        _fail("evaluator executable must be a Windows .exe file")
    _require_windows_pe_executable(executable, name="evaluator executable")
    command_files = evaluator.command_files
    for path in command_files:
        _canonical_file(path, name="evaluator command file")
    roots = tuple(
        dict.fromkeys(
            (
                executable.parent,
                *(path.parent for path in command_files),
            )
        )
    )
    registry = ArtifactRegistry.from_store(store, local_file_roots=roots)
    executable_record = registry.register_file(
        workspace_id=workspace_id,
        idempotency_key="physical-old-new-evaluator-executable",
        path=executable,
        kind="model_evaluator_executable",
    )
    command_artifact_ids: dict[int, str] = {}
    for index, path in enumerate(command_files, start=1):
        record = registry.register_file(
            workspace_id=workspace_id,
            idempotency_key=f"physical-old-new-evaluator-command-file-{index}",
            path=path,
            kind="model_evaluator_command_file",
        )
        command_artifact_ids[index] = record.artifact_id
    command = (
        os.fspath(executable),
        *(os.fspath(path) for path in command_files),
        *evaluator.switches,
    )
    return registry, command, executable_record.artifact_id, command_artifact_ids


def _policy_payload(policy: PromotionPolicy) -> dict[str, object]:
    if type(policy) is not PromotionPolicy:
        raise TypeError("policy must be an exact PromotionPolicy")
    PromotionPolicy.__post_init__(policy)
    return {
        "primary_metric": policy.primary_metric,
        "minimum_improvement": float(policy.minimum_improvement),
        "minimum_replays": policy.minimum_replays,
        "primary_higher_is_better": policy.primary_higher_is_better,
        "guardrails": [
            {
                "metric": item.metric,
                "higher_is_better": item.higher_is_better,
                "max_regression": float(item.max_regression),
            }
            for item in policy.guardrails
        ],
    }


def _sha256_text(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _evaluation_effect_identity(
    *,
    requested_experiment_id: str,
    pilot: PhysicalTrainingPilotReport,
    training_binding_sha256: str,
    champion_binding_sha256: str,
    champion: ModelCandidate,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
    attestor_id: str,
    attestor_sha256: str,
) -> tuple[str, str, str]:
    effect_payload = {
        "schema": "nika-physical-old-new-effect-v1",
        "physical_pilot_evidence_sha256": pilot.evidence_sha256,
        "training_binding_sha256": _sha256_text(
            training_binding_sha256,
            name="training_binding_sha256",
        ),
        "champion_binding_sha256": _sha256_text(
            champion_binding_sha256,
            name="champion_binding_sha256",
        ),
        "champion_candidate_sha256": champion.evidence_sha256,
        "challenger_candidate_sha256": challenger.evidence_sha256,
        "evaluation_set_sha256": evaluation_set.content_sha256,
        "execution_config_sha256": execution_config.evidence_sha256,
        "attestor_id": _require_text(attestor_id, name="attestor_id"),
        "attestor_sha256": _sha256_text(
            attestor_sha256,
            name="attestor_sha256",
        ),
    }
    effect_encoded = json.dumps(
        effect_payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    effect_sha256 = hashlib.sha256(effect_encoded).hexdigest()

    input_payload = {
        "schema": "nika-physical-old-new-input-v1",
        "effect_sha256": effect_sha256,
        "requested_experiment_id": _require_text(
            requested_experiment_id,
            name="requested_experiment_id",
        ),
        "policy": _policy_payload(policy),
        "permission_fingerprint": _require_text(
            permission_fingerprint,
            name="permission_fingerprint",
        ),
    }
    input_encoded = json.dumps(
        input_payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    input_sha256 = hashlib.sha256(input_encoded).hexdigest()
    return (
        f"physical-old-new-effect:{effect_sha256}",
        f"sha256:{input_sha256}",
        f"nika-physical-old-new-{input_sha256}",
    )


def _claim_evaluation_attempt(
    *,
    repository: SQLiteExperimentRepository,
    experiment_id: str,
    champion: ModelCandidate,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
) -> None:
    definition = build_experiment_definition(
        experiment_id=experiment_id,
        champion=champion,
        challengers=(challenger,),
        evaluation_set=evaluation_set,
        execution_config=execution_config,
        policy=policy,
        permission_fingerprint=permission_fingerprint,
    )
    engine = ExperimentEngine(repository)
    try:
        engine.create(definition)
    except ValueError as exc:
        try:
            existing = repository.get(experiment_id)
        except KeyError:
            raise PhysicalEvaluationDriverError(
                "durable physical evaluation attempt could not be claimed"
            ) from exc
        if existing.definition != definition:
            raise PhysicalEvaluationDriverError(
                "durable physical evaluation attempt identity conflicts with existing state"
            ) from exc
        raise PhysicalEvaluationDriverError(
            "durable physical evaluation attempt already exists; model-effect state "
            "may be unknown, so this driver will not repeat champion/challenger effects; "
            "preserve the pilot database and reconcile the attempt before any fresh run"
        ) from exc
    try:
        snapshot = engine.start(experiment_id)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError(
            "durable physical evaluation attempt was claimed but could not enter the "
            "running state; preserve the pilot database and reconcile before retrying"
        ) from exc
    if snapshot.status is not ExperimentStatus.RUNNING or snapshot.observations:
        raise PhysicalEvaluationDriverError(
            "durable physical evaluation attempt claim is not a fresh running snapshot"
        )


def _canonical_report_payload(
    *,
    requested_experiment_id: str,
    pilot: PhysicalTrainingPilotReport,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    comparison: AttestedTrainingComparisonResult,
) -> dict[str, object]:
    result = comparison.revalidated()
    comparison_payload = result.evidence_payload()
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "schema": "nika-physical-old-new-evaluation-report-v1",
        "physical_pilot_evidence_sha256": pilot.evidence_sha256,
        "requested_experiment_id": requested_experiment_id,
        "evaluation_set_sha256": evaluation_set.content_sha256,
        "execution_config_sha256": execution_config.evidence_sha256,
        "comparison_evidence_sha256": result.evidence_sha256,
        "experiment_id": comparison_payload["experiment_id"],
        "experiment_status": comparison_payload["experiment_status"],
        "selected_candidate_id": comparison_payload["selected_candidate_id"],
        "previous_champion_id": comparison_payload["previous_champion_id"],
        "training_binding_sha256": comparison_payload["training_binding_sha256"],
        "champion_benchmark_sha256": comparison_payload["champion_benchmark_sha256"],
        "challenger_benchmark_sha256": comparison_payload["challenger_benchmark_sha256"],
        "attestor_id": comparison_payload["attestor_id"],
        "attestor_sha256": comparison_payload["attestor_sha256"],
        "champion_provider_manifest_sha256": comparison_payload.get(
            "champion_provider_manifest_sha256"
        ),
        "challenger_provider_manifest_sha256": comparison_payload.get(
            "challenger_provider_manifest_sha256"
        ),
    }


def _validate_recovered_report_payload(
    result: object,
    *,
    pilot: PhysicalTrainingPilotReport,
    requested_experiment_id: str,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    experiment_id: str,
    training_binding_sha256: str,
    attestor_id: str,
    attestor_sha256: str,
) -> dict[str, object]:
    if type(result) is not dict or frozenset(result) != _REPORT_KEYS:
        _fail("completed evaluation ledger result is not a canonical report payload")
    if (
        result["schema_version"] != _REPORT_SCHEMA_VERSION
        or result["schema"] != "nika-physical-old-new-evaluation-report-v1"
        or result["physical_pilot_evidence_sha256"] != pilot.evidence_sha256
        or result["requested_experiment_id"] != requested_experiment_id
        or result["evaluation_set_sha256"] != evaluation_set.content_sha256
        or result["execution_config_sha256"] != execution_config.evidence_sha256
        or result["experiment_id"] != experiment_id
        or result["training_binding_sha256"] != training_binding_sha256
        or result["attestor_id"] != attestor_id
        or result["attestor_sha256"] != attestor_sha256
    ):
        _fail("completed evaluation ledger result does not match current physical authority")
    for key in (
        "physical_pilot_evidence_sha256",
        "evaluation_set_sha256",
        "execution_config_sha256",
        "comparison_evidence_sha256",
        "training_binding_sha256",
        "champion_benchmark_sha256",
        "challenger_benchmark_sha256",
        "attestor_sha256",
    ):
        _sha256_text(result[key], name=f"ledger result {key}")
    for key in (
        "champion_provider_manifest_sha256",
        "challenger_provider_manifest_sha256",
    ):
        value = result[key]
        if value is not None:
            _sha256_text(value, name=f"ledger result {key}")
    if result["experiment_status"] not in {"completed", "promoted"}:
        _fail("completed evaluation ledger result is not terminal")
    for key in ("selected_candidate_id", "previous_champion_id", "attestor_id"):
        _require_text(result[key], name=f"ledger result {key}")
    return dict(result)


def _reserve_evaluation_effect(
    *,
    ledger: IdempotencyLedger,
    task_id: str,
    operation_key: str,
    input_fingerprint: str,
) -> tuple[IdempotencyRecord, bool]:
    try:
        return ledger.reserve_once(
            operation_key=operation_key,
            task_id=task_id,
            operation_type="training.physical_old_new_evaluation",
            input_fingerprint=input_fingerprint,
        )
    except IdempotencyConflictError as exc:
        raise PhysicalEvaluationDriverError(
            "the same physical benchmark effects are already bound to different "
            "experiment/policy/permission input; refusing to replay them"
        ) from exc


def _validate_recovered_experiment(
    *,
    repository: SQLiteExperimentRepository,
    experiment_id: str,
    champion: ModelCandidate,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
    report_payload: dict[str, object],
) -> None:
    expected_definition = build_experiment_definition(
        experiment_id=experiment_id,
        champion=champion,
        challengers=(challenger,),
        evaluation_set=evaluation_set,
        execution_config=execution_config,
        policy=policy,
        permission_fingerprint=permission_fingerprint,
    )
    try:
        snapshot = repository.get(experiment_id)
    except KeyError as exc:
        raise PhysicalEvaluationDriverError(
            "completed evaluation ledger has no matching Experiment Engine state"
        ) from exc
    if snapshot.definition != expected_definition:
        _fail("completed evaluation Experiment definition does not match current authority")
    if snapshot.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        _fail("completed evaluation ledger points to a non-terminal Experiment snapshot")
    if (
        snapshot.status.value != report_payload["experiment_status"]
        or snapshot.selected_candidate_id != report_payload["selected_candidate_id"]
        or snapshot.previous_champion_id != report_payload["previous_champion_id"]
    ):
        _fail("completed evaluation ledger conflicts with terminal Experiment decision")


def _mark_evaluation_uncertain(
    ledger: IdempotencyLedger,
    record: IdempotencyRecord,
) -> None:
    try:
        ledger.mark_pending_uncertain_if_matches(
            operation_key=record.operation_key,
            task_id=record.task_id,
            operation_type=record.operation_type,
            input_fingerprint=record.input_fingerprint,
            created_at=record.created_at,
        )
    except (IdempotencyConflictError, KeyError, RuntimeError, TypeError, ValueError):
        _LOG.exception(
            "failed to mark interrupted physical evaluation as uncertain; "
            "its existing ledger reservation still blocks automatic replay"
        )


def _canonical_report_output_path(path: Path) -> tuple[Path, os.stat_result]:
    if type(path) is not type(Path()) or not path.is_absolute():
        _fail("physical evaluation report path must be an absolute canonical platform Path")
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        parent_snapshot = os.lstat(parent)
    except OSError as exc:
        raise PhysicalEvaluationDriverError(
            "physical evaluation report parent directory is unavailable"
        ) from exc
    if (
        resolved_parent != parent
        or stat.S_ISLNK(parent_snapshot.st_mode)
        or _is_reparse(parent_snapshot)
        or not stat.S_ISDIR(parent_snapshot.st_mode)
    ):
        _fail("physical evaluation report parent must be a canonical non-linked directory")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PhysicalEvaluationDriverError(
            "physical evaluation report destination could not be inspected"
        ) from exc
    else:
        _fail("physical evaluation report already exists; refusing to repeat model effects")
    return path, parent_snapshot


def _close_windows_stability_lock(handle: int | None) -> None:
    if handle is None:
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_handle(ctypes.c_void_p(handle))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        pass


def _open_windows_parent_stability_lock(
    path: Path,
    expected_snapshot: os.stat_result,
) -> int | None:
    if os.name != "nt":
        return None
    handle_value: int | None = None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            os.fspath(path),
            0,
            _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
            | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        handle_value = int(handle)
        current = os.lstat(path)
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse(current)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (expected_snapshot.st_dev, expected_snapshot.st_ino)
        ):
            raise OSError("report parent changed while acquiring stability lock")
        return handle_value
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        _close_windows_stability_lock(handle_value)
        raise PhysicalEvaluationDriverError(
            "physical evaluation report parent could not be locked"
        ) from exc


def _open_windows_report_stability_lock(path: Path) -> int | None:
    if os.name != "nt":
        return None
    handle_value: int | None = None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            os.fspath(path),
            _WINDOWS_GENERIC_READ,
            _WINDOWS_FILE_SHARE_READ,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        handle_value = int(handle)
        return handle_value
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        _close_windows_stability_lock(handle_value)
        raise PhysicalEvaluationDriverError(
            "published physical evaluation report could not be locked"
        ) from exc


def _unlink_report_if_owned(
    path: Path,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None:
        return
    try:
        current = os.lstat(path)
    except OSError:
        return
    if (
        stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != expected_identity
    ):
        return
    try:
        path.unlink()
    except OSError:
        pass


def _strict_report_parse_back(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PhysicalEvaluationDriverError(
            "published physical evaluation report is invalid JSON"
        ) from exc
    if type(value) is not dict:
        _fail("published physical evaluation report must be a JSON object")
    return value


def _write_report(path: Path, payload: dict[str, object]) -> None:
    if type(payload) is not dict:
        raise TypeError("physical evaluation report payload must be an exact dict")
    try:
        body = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PhysicalEvaluationDriverError(
            "physical evaluation report is not canonical JSON"
        ) from exc
    if len(body) > _REPORT_MAX_BYTES:
        _fail("physical evaluation report exceeds the output byte limit")
    if _strict_report_parse_back(body) != payload:
        _fail("physical evaluation report does not round-trip through strict JSON")

    destination, parent_before = _canonical_report_output_path(path)
    parent_lock = _open_windows_parent_stability_lock(
        destination.parent,
        parent_before,
    )
    temporary: Path | None = None
    descriptor: int | None = None
    published_identity: tuple[int, int] | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".physical-evaluation-report.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

        temporary_snapshot = os.lstat(temporary)
        if (
            stat.S_ISLNK(temporary_snapshot.st_mode)
            or _is_reparse(temporary_snapshot)
            or not stat.S_ISREG(temporary_snapshot.st_mode)
            or int(getattr(temporary_snapshot, "st_nlink", 1)) != 1
        ):
            _fail("physical evaluation report temporary is not a canonical regular file")
        published_identity = (
            int(temporary_snapshot.st_dev),
            int(temporary_snapshot.st_ino),
        )

        parent_during = os.lstat(destination.parent)
        if (
            stat.S_ISLNK(parent_during.st_mode)
            or _is_reparse(parent_during)
            or not stat.S_ISDIR(parent_during.st_mode)
            or (parent_during.st_dev, parent_during.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
        ):
            _fail("physical evaluation report parent changed during publication")

        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise PhysicalEvaluationDriverError(
                "physical evaluation report already exists"
            ) from exc
        linked_snapshot = os.lstat(destination)
        if (
            stat.S_ISLNK(linked_snapshot.st_mode)
            or _is_reparse(linked_snapshot)
            or not stat.S_ISREG(linked_snapshot.st_mode)
            or (linked_snapshot.st_dev, linked_snapshot.st_ino)
            != published_identity
            or int(getattr(linked_snapshot, "st_nlink", 1)) != 2
        ):
            _fail("physical evaluation report publication changed file identity")

        os.unlink(temporary)
        temporary = None
        destination_lock = _open_windows_report_stability_lock(destination)
        try:
            final_snapshot = os.lstat(destination)
            if (
                stat.S_ISLNK(final_snapshot.st_mode)
                or _is_reparse(final_snapshot)
                or not stat.S_ISREG(final_snapshot.st_mode)
                or (final_snapshot.st_dev, final_snapshot.st_ino)
                != published_identity
                or int(getattr(final_snapshot, "st_nlink", 1)) != 1
            ):
                _fail("published physical evaluation report is not canonical")
            published_body = _read_regular_file(
                destination,
                name="published physical evaluation report",
                max_bytes=_REPORT_MAX_BYTES,
            )
            if not hmac.compare_digest(published_body, body):
                _fail("published physical evaluation report bytes changed")
            if _strict_report_parse_back(published_body) != payload:
                _fail("published physical evaluation report changed during parse-back")

            parent_after = os.lstat(destination.parent)
            if (
                stat.S_ISLNK(parent_after.st_mode)
                or _is_reparse(parent_after)
                or not stat.S_ISDIR(parent_after.st_mode)
                or (parent_after.st_dev, parent_after.st_ino)
                != (parent_before.st_dev, parent_before.st_ino)
            ):
                _fail("physical evaluation report parent changed during publication")
        finally:
            _close_windows_stability_lock(destination_lock)
    except PhysicalEvaluationDriverError:
        _unlink_report_if_owned(destination, published_identity)
        raise
    except OSError as exc:
        _unlink_report_if_owned(destination, published_identity)
        raise PhysicalEvaluationDriverError(
            "physical evaluation report could not be persisted"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        _close_windows_stability_lock(parent_lock)


def _is_windows() -> bool:
    return os.name == "nt"


async def _run_attested_comparison(
    *,
    config: PhysicalEvaluationConfig,
    store: SQLiteStore,
    task: TaskRecord,
    pilot: PhysicalTrainingPilotReport,
    evaluation_set: EvaluationSet,
    training_binding: object,
    champion_binding: object,
    champion: ModelCandidate,
    challenger: ModelCandidate,
    base_descriptor: ModelArtifactDescriptor,
    candidate_descriptor: ModelArtifactDescriptor,
    output_root: Path,
) -> dict[str, object]:
    registry, command, evaluator_artifact_id, command_artifact_ids = _register_evaluator(
        store=store,
        workspace_id=config.workspace_id,
        evaluator=config.evaluator,
    )
    champion_attestor = RegistrySubprocessLoadedModelAttestor(
        command,
        artifact_registry=registry,
        evaluator_artifact_id=evaluator_artifact_id,
        command_artifact_ids=command_artifact_ids,
        candidate_path=os.fspath(config.base_model_path),
        descriptor=base_descriptor,
        allowed_root=os.fspath(config.base_model_path.parent),
        timeout_seconds=config.benchmark.timeout_seconds,
    )
    challenger_attestor = RegistrySubprocessLoadedModelAttestor(
        command,
        artifact_registry=registry,
        evaluator_artifact_id=evaluator_artifact_id,
        command_artifact_ids=command_artifact_ids,
        candidate_path=os.fspath(config.candidate_model_path),
        descriptor=candidate_descriptor,
        allowed_root=os.fspath(output_root),
        timeout_seconds=config.benchmark.timeout_seconds,
    )
    if (
        champion_attestor.attestor_id != challenger_attestor.attestor_id
        or champion_attestor.attestor_sha256 != challenger_attestor.attestor_sha256
    ):
        _fail("champion and challenger evaluator commands do not share one attestor authority")

    _verify_completed_checkpoint(store, task=task, report=pilot)
    operation_key, input_fingerprint, experiment_id = _evaluation_effect_identity(
        requested_experiment_id=config.experiment_id,
        pilot=pilot,
        training_binding_sha256=training_binding.binding_sha256,
        champion_binding_sha256=champion_binding.binding_sha256,
        champion=champion,
        challenger=challenger,
        evaluation_set=evaluation_set,
        execution_config=config.benchmark,
        policy=config.policy,
        permission_fingerprint=config.permission_fingerprint,
        attestor_id=champion_attestor.attestor_id,
        attestor_sha256=champion_attestor.attestor_sha256,
    )
    ledger = IdempotencyLedger(store)
    reservation, created = _reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key=operation_key,
        input_fingerprint=input_fingerprint,
    )
    if not created:
        if reservation.status is IdempotencyStatus.COMPLETED:
            payload = _validate_recovered_report_payload(
                reservation.result,
                pilot=pilot,
                requested_experiment_id=config.experiment_id,
                evaluation_set=evaluation_set,
                execution_config=config.benchmark,
                experiment_id=experiment_id,
                training_binding_sha256=training_binding.binding_sha256,
                attestor_id=champion_attestor.attestor_id,
                attestor_sha256=champion_attestor.attestor_sha256,
            )
            _validate_recovered_experiment(
                repository=SQLiteExperimentRepository(store),
                experiment_id=experiment_id,
                champion=champion,
                challenger=challenger,
                evaluation_set=evaluation_set,
                execution_config=config.benchmark,
                policy=config.policy,
                permission_fingerprint=config.permission_fingerprint,
                report_payload=payload,
            )
            return payload
        raise PhysicalEvaluationDriverError(
            "physical evaluation already has a durable "
            f"{reservation.status.value} side-effect reservation; automatic model-effect "
            "replay is forbidden until that reservation is reconciled"
        )

    repository = SQLiteExperimentRepository(store)
    try:
        _claim_evaluation_attempt(
            repository=repository,
            experiment_id=experiment_id,
            champion=champion,
            challenger=challenger,
            evaluation_set=evaluation_set,
            execution_config=config.benchmark,
            policy=config.policy,
            permission_fingerprint=config.permission_fingerprint,
        )

        _verify_completed_checkpoint(store, task=task, report=pilot)
        champion_result = await run_attested_champion_benchmark(
            binding=champion_binding,
            champion=champion,
            evaluation_set=evaluation_set,
            effect_port=champion_attestor,
            expected_attestor_id=champion_attestor.attestor_id,
            expected_attestor_sha256=champion_attestor.attestor_sha256,
            timeout_seconds=config.benchmark.timeout_seconds,
            temperature=config.benchmark.temperature,
            scorer_id=config.benchmark.scorer_id,
        )

        _verify_completed_checkpoint(store, task=task, report=pilot)
        challenger_result = await run_attested_challenger_benchmark(
            binding=training_binding,
            challenger=challenger,
            evaluation_set=evaluation_set,
            effect_port=challenger_attestor,
            expected_attestor_id=challenger_attestor.attestor_id,
            expected_attestor_sha256=challenger_attestor.attestor_sha256,
            timeout_seconds=config.benchmark.timeout_seconds,
            temperature=config.benchmark.temperature,
            scorer_id=config.benchmark.scorer_id,
        )

        _verify_completed_checkpoint(store, task=task, report=pilot)
        comparison = run_attested_old_vs_new_comparison(
            champion_result=champion_result,
            challenger_result=challenger_result,
            evaluation_set=evaluation_set,
            execution_config=config.benchmark,
            policy=config.policy,
            permission_fingerprint=config.permission_fingerprint,
            experiment_id=experiment_id,
            repository=repository,
        )
        payload = _canonical_report_payload(
            requested_experiment_id=config.experiment_id,
            pilot=pilot,
            evaluation_set=evaluation_set,
            execution_config=config.benchmark,
            comparison=comparison,
        )
        ledger.complete_pending_if_matches(
            operation_key=reservation.operation_key,
            task_id=reservation.task_id,
            operation_type=reservation.operation_type,
            input_fingerprint=reservation.input_fingerprint,
            created_at=reservation.created_at,
            result=payload,
        )
        return payload
    except Exception:
        _mark_evaluation_uncertain(ledger, reservation)
        raise

def run_physical_evaluation_from_config(
    config: PhysicalEvaluationConfig,
) -> dict[str, object]:
    if type(config) is not PhysicalEvaluationConfig:
        raise TypeError("config must be exact PhysicalEvaluationConfig")
    if not _is_windows():
        _fail("physical old-vs-new evaluation driver must execute on Windows")

    output_root = _canonical_directory(
        config.physical_pilot_output_root,
        name="physical_pilot_output_root",
    )
    report_path = output_root / "physical-old-new-evaluation-report.json"
    _canonical_report_output_path(report_path)

    database_path = output_root / "physical-pilot.sqlite3"
    _canonical_file(database_path, name="physical pilot database")
    pilot = _physical_report(output_root)
    if pilot.completed_steps != 2:
        _fail("repository-native physical pilot evaluation requires the canonical two-step pilot")

    candidate_path = config.candidate_model_path.resolve(strict=True)
    if not candidate_path.is_relative_to(output_root):
        _fail("candidate_model_path must remain inside physical_pilot_output_root")

    package_bytes = _read_regular_file(
        config.frozen_package_path,
        name="frozen learning package",
        max_bytes=_FROZEN_PACKAGE_MAX_BYTES,
    )
    try:
        package = FrozenLearningPackage.from_json(
            package_bytes,
            expected_manifest_sha256=pilot.frozen_package_sha256,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError(
            "frozen learning package does not match physical pilot evidence"
        ) from exc

    evaluation_set = _evaluation_set_from_json(
        _read_utf8(
            config.evaluation_set_path,
            name="held-out evaluation set",
            max_bytes=_EVALUATION_SET_MAX_BYTES,
        )
    )
    if evaluation_set.content_sha256 != package.evaluation_set_sha256:
        _fail("held-out evaluation set does not match the frozen learning package")

    base_size = _model_size(config.base_model_path, name="base model artifact")
    _model_size(config.candidate_model_path, name="candidate model artifact")
    base_descriptor = _base_descriptor(
        config.base_model,
        report=pilot,
        size_bytes=base_size,
    )
    candidate_descriptor = _candidate_descriptor(config.candidate_model, report=pilot)

    champion = _candidate(
        candidate_id=config.base_artifact_ref,
        descriptor=base_descriptor,
        evaluator=config.evaluator,
    )
    challenger = _candidate(
        candidate_id=pilot.candidate_artifact_ref,
        descriptor=candidate_descriptor,
        evaluator=config.evaluator,
    )

    store = SQLiteStore(database_path)
    store.initialize()
    task = _find_pilot_task(
        store,
        workspace_id=config.workspace_id,
        job_id=pilot.job_id,
    )
    _verify_completed_checkpoint(store, task=task, report=pilot)

    spec = TrainingJobSpec(
        job_id=pilot.job_id,
        task_id=task.task_id,
        project_id=config.project_id,
        owner_id=config.owner_id,
        base_artifact=ArtifactIdentity(config.base_artifact_ref, pilot.base_sha256),
        frozen_package_sha256=pilot.frozen_package_sha256,
        training_material_sha256=pilot.training_material_sha256,
        scale_authorization_sha256=pilot.scale_authorization_sha256,
        candidate_artifact_ref=pilot.candidate_artifact_ref,
        max_steps=2,
    )
    completed = TrainingRunEvidence(
        job_id=pilot.job_id,
        state=TrainingRunState.COMPLETED,
        next_step=pilot.completed_steps,
        base_artifact=spec.base_artifact,
        frozen_package_sha256=pilot.frozen_package_sha256,
        training_material_sha256=pilot.training_material_sha256,
        scale_authorization_sha256=pilot.scale_authorization_sha256,
        execution_plan_sha256=pilot.execution_plan_sha256,
        job_fingerprint=pilot.job_fingerprint,
        candidate_artifact_ref=pilot.candidate_artifact_ref,
        candidate_sha256=pilot.candidate_sha256,
        checkpoint_id=pilot.completed_checkpoint_id,
    )
    try:
        training_binding = bind_training_result_for_evaluation(
            spec=spec,
            evidence=completed,
            package=package,
            base_path=config.base_model_path,
            base_descriptor=base_descriptor,
            candidate_path=config.candidate_model_path,
            descriptor=candidate_descriptor,
            champion=champion,
            challenger=challenger,
            evaluation_set=evaluation_set,
        )
        champion_binding = bind_champion_for_attested_evaluation(
            training_binding=training_binding,
            champion=champion,
            descriptor=base_descriptor,
            champion_path=config.base_model_path,
            allowed_root=config.base_model_path.parent,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise PhysicalEvaluationDriverError(
            "physical training evidence could not be bound to old-vs-new evaluation"
        ) from exc

    payload = asyncio.run(
        _run_attested_comparison(
            config=config,
            store=store,
            task=task,
            pilot=pilot,
            evaluation_set=evaluation_set,
            training_binding=training_binding,
            champion_binding=champion_binding,
            champion=champion,
            challenger=challenger,
            base_descriptor=base_descriptor,
            candidate_descriptor=candidate_descriptor,
            output_root=output_root,
        )
    )
    _write_report(report_path, payload)
    _LOG.info(
        "physical old-vs-new evaluation completed: experiment_id=%s status=%s",
        payload["experiment_id"],
        payload["experiment_status"],
    )
    return payload


def _read_config(path: Path) -> PhysicalEvaluationConfig:
    raw = _read_regular_file(path, name="physical evaluation config", max_bytes=_CONFIG_MAX_BYTES)
    return PhysicalEvaluationConfig.from_json(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run canonical attested old-vs-new evaluation for a completed Windows PEFT pilot."
        )
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to the local UTF-8 physical-evaluation JSON manifest.",
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
        payload = run_physical_evaluation_from_config(config)
    except (
        KeyError,
        OSError,
        PhysicalEvaluationDriverError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        _LOG.error("physical old-vs-new evaluation failed: %s", exc)
        return 1
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
