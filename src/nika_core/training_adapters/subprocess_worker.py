from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import NoReturn

from nika_core.artifacts import (
    ArtifactLocationKind,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactRegistryError,
    ArtifactVerificationState,
)
from nika_core.training_materials import ResolvedTrainingPackage, TrainingMaterialResolutionError
from nika_core.training_runtime import (
    TrainingJobSpec,
    TrainingStepResult,
    TrainingWorkerError,
    TrainingWorkerFailureEffect,
)

_PROTOCOL_VERSION = 2
_RESUME_ENVELOPE_KEY = "_nika_subprocess"
_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_TIMEOUT_SECONDS = 3600.0
_MAX_ARGUMENT_BYTES = 4096
_MAX_COMMAND_BYTES = 32 * 1024
_MAX_ENVIRONMENT_ENTRIES = 128
_MAX_ENVIRONMENT_FIELD_BYTES = 16 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 4096
_STREAM_JOIN_TIMEOUT_SECONDS = 1.0
_READ_CHUNK_BYTES = 64 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")
_FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {
        "classpath",
        "comspec",
        "dotnet_additional_deps",
        "dotnet_startup_hooks",
        "dyld_framework_path",
        "dyld_insert_libraries",
        "dyld_library_path",
        "java_tool_options",
        "ld_library_path",
        "ld_preload",
        "node_options",
        "path",
        "pathext",
        "perl5lib",
        "perl5opt",
        "rubyopt",
        "_java_options",
    }
)
_SECRET_ENVIRONMENT_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "token",
    }
)


class TrainingSubprocessError(TrainingWorkerError):
    """Safe bounded subprocess failure with explicit external-effect truth."""

    def __init__(self, code: str, *, effect: TrainingWorkerFailureEffect) -> None:
        super().__init__(code, effect=effect)


def _error(code: str, *, effect: TrainingWorkerFailureEffect) -> TrainingSubprocessError:
    return TrainingSubprocessError(code, effect=effect)


def _canonical_json_bytes(
    value: object,
    *,
    max_bytes: int,
    label: str,
    effect: TrainingWorkerFailureEffect,
) -> bytes:
    _validate_json_tree(value, label=label, effect=effect)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error(f"{label}_invalid_json", effect=effect) from exc
    if len(encoded) > max_bytes:
        raise _error(f"{label}_too_large", effect=effect)
    return encoded


def _validate_json_tree(
    value: object,
    *,
    label: str,
    effect: TrainingWorkerFailureEffect,
) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise _error(f"{label}_too_many_values", effect=effect)
        if depth > _MAX_JSON_DEPTH:
            raise _error(f"{label}_too_deep", effect=effect)

        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _error(f"{label}_non_finite", effect=effect)
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise _error(f"{label}_non_string_key", effect=effect)
                visit(child, depth + 1)
            return
        raise _error(f"{label}_non_json_value", effect=effect)

    visit(value, 0)


def _reject_nonstandard_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_sha256(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
    return value


def _validate_positive_byte_limit(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1024 or value > 1024 * 1024:
        raise ValueError(f"{name} must be an integer from 1024 through 1048576")
    return value


def _validate_timeout(value: object) -> float:
    if type(value) is int:
        if value <= 0 or value > _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be greater than 0 and at most 3600")
        return float(value)
    if type(value) is not float:
        raise ValueError("timeout_seconds must be a finite positive number")
    if not math.isfinite(value) or value <= 0 or value > _MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds must be greater than 0 and at most 3600")
    return value


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)):
        raise TypeError("command must be a sequence of arguments, not a shell string")
    normalized = tuple(command)
    if not normalized:
        raise ValueError("command must not be empty")

    total_bytes = 0
    for index, argument in enumerate(normalized):
        if (
            type(argument) is not str
            or "\x00" in argument
            or any(not character.isprintable() for character in argument)
        ):
            raise ValueError("command arguments must be printable NUL-free strings")
        try:
            encoded_length = len(argument.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise ValueError("command arguments must be valid UTF-8 text") from exc
        if encoded_length > _MAX_ARGUMENT_BYTES:
            raise ValueError("command argument exceeds the configured byte limit")
        total_bytes += encoded_length
        if index == 0 and (not argument or not os.path.isabs(argument)):
            raise ValueError("training executable must use an absolute path")
    if total_bytes > _MAX_COMMAND_BYTES:
        raise ValueError("command exceeds the configured byte limit")
    return normalized


def _validate_command_artifact_ids(
    command: tuple[str, ...],
    *,
    trainer_artifact_id: str,
    command_artifact_ids: Mapping[int, str] | None,
) -> dict[int, str]:
    result = {
        0: _validate_sha256(trainer_artifact_id, name="trainer_artifact_id"),
    }
    if command_artifact_ids is not None:
        for index, artifact_id in command_artifact_ids.items():
            if type(index) is not int or not 1 <= index < len(command):
                raise ValueError(
                    "command artifact indexes must identify non-executable argv entries"
                )
            result[index] = _validate_sha256(
                artifact_id,
                name=f"command_artifact_ids[{index}]",
            )
            if not os.path.isabs(command[index]):
                raise ValueError("command artifact bindings require absolute file arguments")

    for index, argument in enumerate(command[1:], start=1):
        if os.path.isabs(argument):
            if index not in result:
                raise ValueError(
                    "absolute command file arguments must be bound through Artifact Registry"
                )
            continue
        separators = tuple(
            separator for separator in (os.sep, os.altsep) if separator is not None
        )
        if any(separator in argument for separator in separators):
            raise ValueError("relative path command arguments are forbidden")
        if (
            len(argument) < 2
            or not argument.startswith("-")
            or any(
                not (
                    ord(character) < 128
                    and (character.isalnum() or character in "-_")
                )
                for character in argument[1:]
            )
        ):
            raise ValueError(
                "unbound command arguments must be simple option switches; "
                "use stdin for trainer data"
            )
    return result


def _command_sha256(
    command: tuple[str, ...],
    command_artifact_ids: Mapping[int, str],
    *,
    environment: Mapping[str, str],
) -> str:
    payload = {
        "argv": list(command),
        "artifact_ids": {
            str(index): command_artifact_ids[index]
            for index in sorted(command_artifact_ids)
        },
        "environment": {key: environment[key] for key in sorted(environment)},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"nika-training-command-v2\x00" + encoded).hexdigest()


def _environment_key_contains_credential_name(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    if not normalized:
        return False
    parts = tuple(part for part in normalized.split("_") if part)
    if "apikey" in parts:
        return True
    if any(part in _SECRET_ENVIRONMENT_TOKENS for part in parts):
        return True
    return any(
        left == "api" and right == "key"
        for left, right in zip(parts, parts[1:], strict=False)
    )


def _validate_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return {}
    if len(environment) > _MAX_ENVIRONMENT_ENTRIES:
        raise ValueError("environment contains too many entries")

    result: dict[str, str] = {}
    normalized_keys: set[str] = set()
    for key, value in environment.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError("environment keys and values must be strings")
        if (
            not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
            or any(not character.isprintable() for character in key)
            or any(not character.isprintable() for character in value)
        ):
            raise ValueError("environment contains an invalid key or value")
        normalized_key = key.casefold()
        if normalized_key in normalized_keys:
            raise ValueError("environment keys must be unique ignoring case")
        normalized_keys.add(normalized_key)
        if (
            normalized_key in _FORBIDDEN_ENVIRONMENT_KEYS
            or normalized_key.startswith("python")
            or normalized_key.startswith("ld_")
            or normalized_key.startswith("dyld_")
        ):
            raise ValueError("environment may not alter runtime or loader authority")
        if _environment_key_contains_credential_name(key):
            raise ValueError("training environment must not contain credential material")
        try:
            key_bytes = key.encode("utf-8", errors="strict")
            value_bytes = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("environment must use valid UTF-8 text") from exc
        if len(key_bytes) > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment key exceeds the configured byte limit")
        if len(value_bytes) > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment value exceeds the configured byte limit")
        result[key] = value
    return result


def _job_identity(
    spec: TrainingJobSpec,
    *,
    command_sha256: str,
) -> dict[str, object]:
    return {
        "base_artifact": {
            "artifact_ref": spec.base_artifact.artifact_ref,
            "sha256": spec.base_artifact.sha256,
        },
        "candidate_artifact_ref": spec.candidate_artifact_ref,
        "command_sha256": command_sha256,
        "frozen_package_sha256": spec.frozen_package_sha256,
        "job_id": spec.job_id,
        "max_steps": spec.max_steps,
        "owner_id": spec.owner_id,
        "project_id": spec.project_id,
        "resource_scope": spec.resource_scope,
        "task_id": spec.task_id,
        "training_material_sha256": spec.training_material_sha256,
    }


def _job_fingerprint(spec: TrainingJobSpec, *, command_sha256: str) -> str:
    identity = _canonical_json_bytes(
        _job_identity(spec, command_sha256=command_sha256),
        max_bytes=_DEFAULT_MAX_REQUEST_BYTES,
        label="training_job_identity",
        effect=TrainingWorkerFailureEffect.NO_EFFECT,
    )
    return hashlib.sha256(b"nika-training-job-v2\x00" + identity).hexdigest()


def _step_id(job_fingerprint: str, trainer_sha256: str, step_index: int) -> str:
    material = (
        f"nika-training-step-v2\x00{job_fingerprint}\x00{trainer_sha256}\x00{step_index}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _normalized_executable_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _snapshot_spec(spec: TrainingJobSpec) -> TrainingJobSpec:
    if type(spec) is not TrainingJobSpec:
        raise _error(
            "training_spec_invalid_type",
            effect=TrainingWorkerFailureEffect.NO_EFFECT,
        )
    try:
        base = spec.base_artifact
        if type(base) is not ArtifactIdentity:
            raise TypeError("base_artifact must be an exact ArtifactIdentity")
        return TrainingJobSpec(
            job_id=spec.job_id,
            task_id=spec.task_id,
            project_id=spec.project_id,
            owner_id=spec.owner_id,
            base_artifact=ArtifactIdentity(
                artifact_ref=base.artifact_ref,
                sha256=base.sha256,
            ),
            frozen_package_sha256=spec.frozen_package_sha256,
            training_material_sha256=spec.training_material_sha256,
            candidate_artifact_ref=spec.candidate_artifact_ref,
            max_steps=spec.max_steps,
            resource_scope=spec.resource_scope,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error(
            "training_spec_invalid",
            effect=TrainingWorkerFailureEffect.NO_EFFECT,
        ) from exc


def _materials_match_spec(
    spec: TrainingJobSpec,
    training_materials: ResolvedTrainingPackage,
) -> bool:
    try:
        return (
            hmac.compare_digest(
                training_materials.training_material_sha256,
                spec.training_material_sha256,
            )
            and hmac.compare_digest(
                training_materials.evidence.package_manifest_sha256,
                spec.frozen_package_sha256,
            )
            and hmac.compare_digest(
                training_materials.evidence.base_artifact_sha256,
                spec.base_artifact.sha256,
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _training_material_request(
    training_materials: ResolvedTrainingPackage,
) -> dict[str, object]:
    return {
        "base_artifact_sha256": training_materials.evidence.base_artifact_sha256,
        "package_manifest_sha256": training_materials.evidence.package_manifest_sha256,
        "training_material_sha256": training_materials.training_material_sha256,
        "materials": [
            {
                "artifact_sha256": material.evidence.artifact_sha256,
                "byte_count": material.evidence.byte_count,
                "path": os.fspath(material.path),
                "split": material.evidence.split.value,
            }
            for material in training_materials.materials
        ],
    }


class SubprocessTrainingWorker:
    """Shell-free TrainingWorkerPort adapter for one Registry-authorized trainer command.

    Durable resume state binds the exact command vector, Registry-bound command artifacts,
    explicit sterile environment, trainer digest and frozen/material job identities. Absolute
    command-file arguments must have Artifact Registry authority; relative path arguments are
    forbidden. Physical training
    paths remain transient request data only. Immediately before spawn, the canonical Registry
    verifies every bound command artifact and ResolvedTrainingPackage re-binds all input bytes.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        artifact_registry: ArtifactRegistry,
        trainer_artifact_id: str,
        command_artifact_ids: Mapping[int, str] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._command = _validate_command(command)
        if type(artifact_registry) is not ArtifactRegistry:
            raise TypeError("artifact_registry must be the canonical ArtifactRegistry")
        self._artifact_registry = artifact_registry
        self._command_artifact_ids = _validate_command_artifact_ids(
            self._command,
            trainer_artifact_id=trainer_artifact_id,
            command_artifact_ids=command_artifact_ids,
        )
        self._trainer_artifact_id = self._command_artifact_ids[0]
        self._environment = _validate_environment(environment)
        self._command_sha256 = _command_sha256(
            self._command,
            self._command_artifact_ids,
            environment=self._environment,
        )
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._max_request_bytes = _validate_positive_byte_limit(
            max_request_bytes, name="max_request_bytes"
        )
        self._max_response_bytes = _validate_positive_byte_limit(
            max_response_bytes, name="max_response_bytes"
        )

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        canonical_spec = _snapshot_spec(spec)
        if (
            type(step_index) is not int
            or step_index < 0
            or step_index >= canonical_spec.max_steps
        ):
            raise _error("step_index_out_of_bounds", effect=TrainingWorkerFailureEffect.NO_EFFECT)
        if type(resume_state) is not dict:
            raise _error("resume_state_invalid_type", effect=TrainingWorkerFailureEffect.NO_EFFECT)
        if type(training_materials) is not ResolvedTrainingPackage:
            raise _error(
                "training_materials_invalid_type",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if not _materials_match_spec(canonical_spec, training_materials):
            raise _error(
                "training_material_identity_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )

        command_records = self._get_command_records()
        trainer_record = command_records[0]
        trainer_sha256 = trainer_record.sha256
        job_fingerprint = _job_fingerprint(
            canonical_spec,
            command_sha256=self._command_sha256,
        )
        trainer_state, previous_step_id = self._unwrap_resume_state(
            resume_state=resume_state,
            job_fingerprint=job_fingerprint,
            command_sha256=self._command_sha256,
            trainer_sha256=trainer_sha256,
            step_index=step_index,
        )
        current_step_id = _step_id(job_fingerprint, trainer_sha256, step_index)
        request = {
            "command_artifacts": [
                {
                    "argument_index": index,
                    "artifact_id": record.artifact_id,
                    "sha256": record.sha256,
                }
                for index, record in sorted(command_records.items())
            ],
            "command_sha256": self._command_sha256,
            "job": _job_identity(canonical_spec, command_sha256=self._command_sha256),
            "job_fingerprint": job_fingerprint,
            "previous_step_id": previous_step_id,
            "protocol_version": _PROTOCOL_VERSION,
            "resume_state": trainer_state,
            "step_id": current_step_id,
            "step_index": step_index,
            "trainer_artifact_id": trainer_record.artifact_id,
            "trainer_sha256": trainer_sha256,
            "training_materials": _training_material_request(training_materials),
        }
        request_bytes = _canonical_json_bytes(
            request,
            max_bytes=self._max_request_bytes,
            label="training_subprocess_request",
            effect=TrainingWorkerFailureEffect.NO_EFFECT,
        )

        self._verify_command_artifacts(command_records)
        try:
            training_materials.reverify()
        except TrainingMaterialResolutionError as exc:
            raise _error(
                "training_material_verification_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc

        stdout = self._execute(
            request_bytes + b"\n",
            expected_command_records=command_records,
            expected_training_materials=training_materials,
        )
        response = self._parse_response(stdout, expected_step_id=current_step_id)
        completed = response["completed"]
        candidate_sha256 = response["candidate_sha256"]
        trainer_resume_state = response["resume_state"]
        assert type(completed) is bool
        assert candidate_sha256 is None or type(candidate_sha256) is str
        assert type(trainer_resume_state) is dict

        wrapped_resume_state = {
            _RESUME_ENVELOPE_KEY: {
                "command_sha256": self._command_sha256,
                "job_fingerprint": job_fingerprint,
                "last_step_id": current_step_id,
                "protocol_version": _PROTOCOL_VERSION,
                "trainer_artifact_id": trainer_record.artifact_id,
                "trainer_sha256": trainer_sha256,
                "trainer_state": trainer_resume_state,
            }
        }
        _canonical_json_bytes(
            wrapped_resume_state,
            max_bytes=self._max_request_bytes,
            label="training_resume_state",
            effect=TrainingWorkerFailureEffect.UNKNOWN,
        )
        try:
            return TrainingStepResult(
                resume_state=wrapped_resume_state,
                completed=completed,
                candidate_sha256=candidate_sha256,
            )
        except ValueError as exc:
            raise _error(
                "training_subprocess_invalid_result_evidence",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            ) from exc

    def _get_command_records(self) -> dict[int, ArtifactRecord]:
        records: dict[int, ArtifactRecord] = {}
        for index, artifact_id in sorted(self._command_artifact_ids.items()):
            try:
                record = self._artifact_registry.get(artifact_id)
            except (ArtifactRegistryError, ValueError) as exc:
                raise _error(
                    "command_artifact_lookup_failed",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                ) from exc
            if type(record) is not ArtifactRecord:
                raise _error(
                    "command_artifact_invalid_record",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            if record.location_kind is not ArtifactLocationKind.LOCAL_FILE:
                raise _error(
                    "command_artifact_not_local_file",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            expected_kind = "training_executable" if index == 0 else "training_command_file"
            if record.kind != expected_kind:
                raise _error(
                    "command_artifact_kind_mismatch",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            if _normalized_executable_path(self._command[index]) != _normalized_executable_path(
                record.locator
            ):
                raise _error(
                    "command_artifact_argument_mismatch",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            records[index] = record
        return records

    def _verify_command_artifacts(
        self,
        expected_records: Mapping[int, ArtifactRecord],
    ) -> None:
        for expected in expected_records.values():
            try:
                verification = self._artifact_registry.verify(expected.artifact_id)
                current = self._artifact_registry.get(expected.artifact_id)
            except (ArtifactRegistryError, ValueError) as exc:
                raise _error(
                    "command_artifact_verification_failed",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                ) from exc
            if type(current) is not ArtifactRecord or current != expected:
                raise _error(
                    "command_artifact_record_changed",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            if verification.state is not ArtifactVerificationState.VERIFIED:
                raise _error(
                    "command_artifact_not_verified",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            if (
                verification.expected_sha256 != expected.sha256
                or verification.actual_sha256 != expected.sha256
                or verification.expected_size_bytes != expected.size_bytes
                or verification.actual_size_bytes != expected.size_bytes
            ):
                raise _error(
                    "command_artifact_evidence_mismatch",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )

    def _unwrap_resume_state(
        self,
        *,
        resume_state: dict[str, object],
        job_fingerprint: str,
        command_sha256: str,
        trainer_sha256: str,
        step_index: int,
    ) -> tuple[dict[str, object], str | None]:
        _canonical_json_bytes(
            resume_state,
            max_bytes=self._max_request_bytes,
            label="training_resume_state",
            effect=TrainingWorkerFailureEffect.NO_EFFECT,
        )
        if step_index == 0:
            if resume_state:
                raise _error(
                    "initial_step_unexpected_resume_state",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            return {}, None

        if set(resume_state) != {_RESUME_ENVELOPE_KEY}:
            raise _error(
                "resume_state_invalid_envelope",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        envelope = resume_state[_RESUME_ENVELOPE_KEY]
        if type(envelope) is not dict:
            raise _error(
                "resume_state_invalid_envelope",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        expected_keys = {
            "command_sha256",
            "job_fingerprint",
            "last_step_id",
            "protocol_version",
            "trainer_artifact_id",
            "trainer_sha256",
            "trainer_state",
        }
        if set(envelope) != expected_keys:
            raise _error(
                "resume_state_invalid_envelope",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if (
            type(envelope["protocol_version"]) is not int
            or envelope["protocol_version"] != _PROTOCOL_VERSION
        ):
            raise _error(
                "resume_state_unsupported_protocol",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if envelope["command_sha256"] != command_sha256:
            raise _error(
                "resume_state_command_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if envelope["job_fingerprint"] != job_fingerprint:
            raise _error(
                "resume_state_job_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if envelope["trainer_artifact_id"] != self._trainer_artifact_id:
            raise _error(
                "resume_state_trainer_artifact_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if envelope["trainer_sha256"] != trainer_sha256:
            raise _error(
                "resume_state_trainer_digest_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )

        expected_previous_step_id = _step_id(job_fingerprint, trainer_sha256, step_index - 1)
        if envelope["last_step_id"] != expected_previous_step_id:
            raise _error(
                "resume_state_previous_step_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        trainer_state = envelope["trainer_state"]
        if type(trainer_state) is not dict:
            raise _error(
                "trainer_resume_state_invalid_type",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        return trainer_state, expected_previous_step_id

    @staticmethod
    def _kill_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            return

    @staticmethod
    def _close_pipe(pipe: object) -> None:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            return

    @staticmethod
    def _reap_process(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return

    def _execute(
        self,
        request_bytes: bytes,
        *,
        expected_command_records: Mapping[int, ArtifactRecord],
        expected_training_materials: ResolvedTrainingPackage,
    ) -> bytes:
        deadline = time.monotonic() + self._timeout_seconds
        try:
            process = subprocess.Popen(
                self._command,
                env=dict(self._environment),
                shell=False,
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise _error(
                "training_subprocess_executable_not_found",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc
        except PermissionError as exc:
            raise _error(
                "training_subprocess_executable_not_executable",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc
        except OSError as exc:
            raise _error(
                "training_subprocess_start_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc

        if process.stdin is None or process.stdout is None:
            self._kill_process(process)
            self._reap_process(process)
            raise _error(
                "training_subprocess_streams_unavailable",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )

        try:
            self._verify_command_artifacts(expected_command_records)
        except TrainingSubprocessError as exc:
            self._close_pipe(process.stdin)
            self._close_pipe(process.stdout)
            self._kill_process(process)
            self._reap_process(process)
            raise _error(
                "command_artifact_changed_after_process_start",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            ) from exc

        try:
            expected_training_materials.reverify()
        except TrainingMaterialResolutionError as exc:
            self._close_pipe(process.stdin)
            self._close_pipe(process.stdout)
            self._kill_process(process)
            self._reap_process(process)
            raise _error(
                "training_material_changed_after_process_start",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            ) from exc

        remaining_timeout = deadline - time.monotonic()
        if remaining_timeout <= 0:
            self._close_pipe(process.stdin)
            self._close_pipe(process.stdout)
            self._kill_process(process)
            self._reap_process(process)
            raise _error(
                "training_subprocess_timeout",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )

        stdin = process.stdin
        stdout = process.stdout
        captured = bytearray()
        overflow = threading.Event()
        read_failed = threading.Event()
        write_failed = threading.Event()

        def read_stdout() -> None:
            try:
                while True:
                    remaining = self._max_response_bytes + 1 - len(captured)
                    if remaining <= 0:
                        overflow.set()
                        self._kill_process(process)
                        return
                    chunk = stdout.read(min(_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        return
                    captured.extend(chunk)
                    if len(captured) > self._max_response_bytes:
                        overflow.set()
                        self._kill_process(process)
                        return
            except (OSError, ValueError):
                read_failed.set()
                self._kill_process(process)

        def write_stdin() -> None:
            try:
                stdin.write(request_bytes)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                write_failed.set()
            finally:
                self._close_pipe(stdin)

        reader = threading.Thread(target=read_stdout, name="nika-training-stdout", daemon=True)
        writer = threading.Thread(target=write_stdin, name="nika-training-stdin", daemon=True)
        reader.start()
        writer.start()

        timed_out: subprocess.TimeoutExpired | None = None
        try:
            returncode = process.wait(timeout=remaining_timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = exc
            self._kill_process(process)
            try:
                returncode = process.wait(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                returncode = process.returncode

        reader.join(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)
        writer.join(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)

        if reader.is_alive():
            self._close_pipe(stdout)
            reader.join(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)
        if writer.is_alive():
            self._close_pipe(stdin)
            writer.join(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)

        self._close_pipe(stdout)

        if timed_out is not None:
            raise _error(
                "training_subprocess_timeout",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            ) from timed_out
        if reader.is_alive() or writer.is_alive():
            self._kill_process(process)
            raise _error(
                "training_subprocess_streams_stuck",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if overflow.is_set():
            raise _error(
                "training_subprocess_response_too_large",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if read_failed.is_set():
            raise _error(
                "training_subprocess_response_read_failed",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if returncode != 0:
            raise _error(
                "training_subprocess_nonzero_exit",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if write_failed.is_set():
            raise _error(
                "training_subprocess_request_write_failed",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        return bytes(captured)

    def _parse_response(
        self, raw_response: bytes, *, expected_step_id: str
    ) -> dict[str, object]:
        try:
            text = raw_response.decode("utf-8", errors="strict")
            response = json.loads(
                text,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonstandard_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise _error(
                "training_subprocess_invalid_json_response",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            ) from exc

        if type(response) is not dict:
            raise _error(
                "training_subprocess_response_not_object",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        expected_keys = {
            "candidate_sha256",
            "completed",
            "protocol_version",
            "resume_state",
            "step_id",
        }
        if set(response) != expected_keys:
            raise _error(
                "training_subprocess_response_unexpected_fields",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if (
            type(response["protocol_version"]) is not int
            or response["protocol_version"] != _PROTOCOL_VERSION
        ):
            raise _error(
                "training_subprocess_unsupported_protocol",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if type(response["step_id"]) is not str or response["step_id"] != expected_step_id:
            raise _error(
                "training_subprocess_wrong_step_identity",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if type(response["completed"]) is not bool:
            raise _error(
                "training_subprocess_invalid_completed_flag",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if type(response["resume_state"]) is not dict:
            raise _error(
                "training_subprocess_invalid_resume_state",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )

        candidate_sha256 = response["candidate_sha256"]
        if candidate_sha256 is not None and type(candidate_sha256) is not str:
            raise _error(
                "training_subprocess_invalid_candidate_digest_type",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )
        if response["completed"] is False and candidate_sha256 is not None:
            raise _error(
                "training_subprocess_incomplete_candidate_digest",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            )

        _canonical_json_bytes(
            response["resume_state"],
            max_bytes=max(1024, self._max_request_bytes // 2),
            label="trainer_resume_state",
            effect=TrainingWorkerFailureEffect.UNKNOWN,
        )
        return response
