from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import subprocess
import threading
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
        if type(argument) is not str or "\x00" in argument:
            raise ValueError("command arguments must be NUL-free strings")
        encoded_length = len(argument.encode("utf-8"))
        if encoded_length > _MAX_ARGUMENT_BYTES:
            raise ValueError("command argument exceeds the configured byte limit")
        total_bytes += encoded_length
        if index == 0 and (not argument or not os.path.isabs(argument)):
            raise ValueError("training executable must use an absolute path")
    if total_bytes > _MAX_COMMAND_BYTES:
        raise ValueError("command exceeds the configured byte limit")
    return normalized


def _validate_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return {}
    if len(environment) > _MAX_ENVIRONMENT_ENTRIES:
        raise ValueError("environment contains too many entries")

    result: dict[str, str] = {}
    for key, value in environment.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError("environment keys and values must be strings")
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError("environment contains an invalid key or value")
        if len(key.encode("utf-8")) > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment key exceeds the configured byte limit")
        if len(value.encode("utf-8")) > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment value exceeds the configured byte limit")
        result[key] = value
    return result


def _job_identity(spec: TrainingJobSpec) -> dict[str, object]:
    return {
        "base_artifact": {
            "artifact_ref": spec.base_artifact.artifact_ref,
            "sha256": spec.base_artifact.sha256,
        },
        "candidate_artifact_ref": spec.candidate_artifact_ref,
        "frozen_package_sha256": spec.frozen_package_sha256,
        "job_id": spec.job_id,
        "max_steps": spec.max_steps,
        "owner_id": spec.owner_id,
        "project_id": spec.project_id,
        "resource_scope": spec.resource_scope,
        "task_id": spec.task_id,
        "training_material_sha256": spec.training_material_sha256,
    }


def _job_fingerprint(spec: TrainingJobSpec) -> str:
    identity = _canonical_json_bytes(
        _job_identity(spec),
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
    """Shell-free TrainingWorkerPort adapter for one Registry-authorized trainer executable.

    Durable resume state binds the exact trainer digest and the exact frozen/material job
    identities. Physical training paths remain transient request data only. Immediately
    before spawn, the canonical Artifact Registry verifies the executable and the canonical
    ResolvedTrainingPackage verifier re-binds every input path to frozen bytes.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        artifact_registry: ArtifactRegistry,
        trainer_artifact_id: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._command = _validate_command(command)
        if type(artifact_registry) is not ArtifactRegistry:
            raise TypeError("artifact_registry must be the canonical ArtifactRegistry")
        self._artifact_registry = artifact_registry
        self._trainer_artifact_id = _validate_sha256(
            trainer_artifact_id, name="trainer_artifact_id"
        )
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._environment = _validate_environment(environment)
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
        if type(step_index) is not int or step_index < 0 or step_index >= spec.max_steps:
            raise _error("step_index_out_of_bounds", effect=TrainingWorkerFailureEffect.NO_EFFECT)
        if type(resume_state) is not dict:
            raise _error("resume_state_invalid_type", effect=TrainingWorkerFailureEffect.NO_EFFECT)
        if type(training_materials) is not ResolvedTrainingPackage:
            raise _error(
                "training_materials_invalid_type",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if not _materials_match_spec(spec, training_materials):
            raise _error(
                "training_material_identity_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )

        trainer_record = self._get_trainer_record()
        trainer_sha256 = trainer_record.sha256
        job_fingerprint = _job_fingerprint(spec)
        trainer_state, previous_step_id = self._unwrap_resume_state(
            resume_state=resume_state,
            job_fingerprint=job_fingerprint,
            trainer_sha256=trainer_sha256,
            step_index=step_index,
        )
        current_step_id = _step_id(job_fingerprint, trainer_sha256, step_index)
        request = {
            "job": _job_identity(spec),
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

        self._verify_trainer_artifact(trainer_record)
        try:
            training_materials.reverify()
        except TrainingMaterialResolutionError as exc:
            raise _error(
                "training_material_verification_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc

        stdout = self._execute(request_bytes + b"\n")
        response = self._parse_response(stdout, expected_step_id=current_step_id)
        completed = response["completed"]
        candidate_sha256 = response["candidate_sha256"]
        trainer_resume_state = response["resume_state"]
        assert type(completed) is bool
        assert candidate_sha256 is None or type(candidate_sha256) is str
        assert type(trainer_resume_state) is dict

        wrapped_resume_state = {
            _RESUME_ENVELOPE_KEY: {
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

    def _get_trainer_record(self) -> ArtifactRecord:
        try:
            record = self._artifact_registry.get(self._trainer_artifact_id)
        except (ArtifactRegistryError, ValueError) as exc:
            raise _error(
                "trainer_artifact_lookup_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc
        if type(record) is not ArtifactRecord:
            raise _error(
                "trainer_artifact_invalid_record",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if record.location_kind is not ArtifactLocationKind.LOCAL_FILE:
            raise _error(
                "trainer_artifact_not_local_file",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if _normalized_executable_path(self._command[0]) != _normalized_executable_path(
            record.locator
        ):
            raise _error(
                "trainer_artifact_command_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        return record

    def _verify_trainer_artifact(self, expected: ArtifactRecord) -> None:
        try:
            verification = self._artifact_registry.verify(expected.artifact_id)
            current = self._artifact_registry.get(expected.artifact_id)
        except (ArtifactRegistryError, ValueError) as exc:
            raise _error(
                "trainer_artifact_verification_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc
        if type(current) is not ArtifactRecord or current != expected:
            raise _error(
                "trainer_artifact_record_changed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if verification.state is not ArtifactVerificationState.VERIFIED:
            raise _error(
                "trainer_artifact_not_verified",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if (
            verification.expected_sha256 != expected.sha256
            or verification.actual_sha256 != expected.sha256
            or verification.expected_size_bytes != expected.size_bytes
            or verification.actual_size_bytes != expected.size_bytes
        ):
            raise _error(
                "trainer_artifact_evidence_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )

    def _unwrap_resume_state(
        self,
        *,
        resume_state: dict[str, object],
        job_fingerprint: str,
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

    def _execute(self, request_bytes: bytes) -> bytes:
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
            raise _error(
                "training_subprocess_streams_unavailable",
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
            returncode = process.wait(timeout=self._timeout_seconds)
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
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
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
