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
from nika_core.training_materials import (
    ResolvedTrainingPackage,
    TrainingMaterialResolutionError,
)
from nika_core.training_runtime import (
    TrainingJobSpec,
    TrainingStepResult,
    TrainingWorkerError,
    TrainingWorkerFailureEffect,
)

_PROTOCOL_VERSION = 2
_RESUME_ENVELOPE_KEY = "_nika_subprocess"
_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
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
    """Secret-minimized subprocess failure with explicit trainer-effect truth."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        effect: TrainingWorkerFailureEffect,
    ) -> None:
        super().__init__(code, effect=effect)
        self.args = (message,)


def _boundary_error(
    message: str,
    *,
    code: str,
    effect: TrainingWorkerFailureEffect,
) -> TrainingSubprocessError:
    return TrainingSubprocessError(message, code=code, effect=effect)


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
        raise _boundary_error(
            f"{label} is not valid canonical JSON",
            code="invalid_json",
            effect=effect,
        ) from exc
    if len(encoded) > max_bytes:
        raise _boundary_error(
            f"{label} exceeds the configured byte limit",
            code="json_byte_limit",
            effect=effect,
        )
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
            raise _boundary_error(
                f"{label} contains too many JSON values",
                code="json_node_limit",
                effect=effect,
            )
        if depth > _MAX_JSON_DEPTH:
            raise _boundary_error(
                f"{label} exceeds the JSON nesting limit",
                code="json_depth_limit",
                effect=effect,
            )

        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _boundary_error(
                    f"{label} contains a non-finite number",
                    code="json_non_finite",
                    effect=effect,
                )
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise _boundary_error(
                        f"{label} contains a non-string JSON key",
                        code="json_key_type",
                        effect=effect,
                    )
                visit(child, depth + 1)
            return
        raise _boundary_error(
            f"{label} contains a non-JSON value",
            code="json_value_type",
            effect=effect,
        )

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


def _validate_artifact_id(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError("trainer_artifact_id must be a lowercase 64-character hex digest")
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
        label="training job identity",
        effect=TrainingWorkerFailureEffect.NO_EFFECT,
    )
    return hashlib.sha256(b"nika-training-job-v2\x00" + identity).hexdigest()


def _step_id(
    job_fingerprint: str,
    trainer_artifact_id: str,
    trainer_sha256: str,
    step_index: int,
) -> str:
    material = (
        "nika-training-step-v2\x00"
        f"{job_fingerprint}\x00{trainer_artifact_id}\x00{trainer_sha256}\x00{step_index}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _material_request(materials: ResolvedTrainingPackage) -> list[dict[str, object]]:
    return [
        {
            "artifact_sha256": item.evidence.artifact_sha256,
            "byte_count": item.evidence.byte_count,
            "path": str(item.path),
            "split": item.evidence.split.value,
        }
        for item in materials.materials
    ]


class SubprocessTrainingWorker:
    """Shell-free adapter for one registry-authorized local trainer artifact.

    The trainer artifact is a canonical Artifact Registry local-file record whose exact
    locator must occur in the configured argv. The registry and training-material
    authorities are reverified immediately before process creation. The subprocess gets
    only bounded canonical JSON and an explicit environment. Failures before a process
    exists are NO_EFFECT; once Popen succeeds, every failure is conservatively UNKNOWN.
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
            raise TypeError("artifact_registry must be the exact canonical ArtifactRegistry")
        self._artifact_registry = artifact_registry
        self._trainer_artifact_id = _validate_artifact_id(trainer_artifact_id)
        self._trainer_record = self._load_initial_trainer_record()
        if self._command.count(self._trainer_record.locator) != 1:
            raise ValueError(
                "registered trainer artifact path must occur exactly once in the command"
            )

        self._trainer_sha256 = self._trainer_record.sha256
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._environment = _validate_environment(environment)
        self._max_request_bytes = _validate_positive_byte_limit(
            max_request_bytes,
            name="max_request_bytes",
        )
        self._max_response_bytes = _validate_positive_byte_limit(
            max_response_bytes,
            name="max_response_bytes",
        )

    def _load_initial_trainer_record(self) -> ArtifactRecord:
        try:
            record = self._artifact_registry.get(self._trainer_artifact_id)
        except ArtifactRegistryError as exc:
            raise ValueError("trainer artifact is not registered") from exc
        if record.location_kind is not ArtifactLocationKind.LOCAL_FILE:
            raise ValueError("trainer artifact must be a verifiable local file")
        if not os.path.isabs(record.locator):
            raise ValueError("registered trainer artifact path must be absolute")
        return record

    def _verify_trainer_before_effect(self) -> None:
        try:
            current = self._artifact_registry.get(self._trainer_artifact_id)
            if current != self._trainer_record:
                raise _boundary_error(
                    "trainer artifact registry identity changed",
                    code="trainer_identity_changed",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            verification = self._artifact_registry.verify(self._trainer_artifact_id)
        except TrainingSubprocessError:
            raise
        except ArtifactRegistryError as exc:
            raise _boundary_error(
                "trainer artifact verification failed",
                code="trainer_verification_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc

        if (
            verification.state is not ArtifactVerificationState.VERIFIED
            or verification.actual_sha256 != self._trainer_record.sha256
            or verification.actual_size_bytes != self._trainer_record.size_bytes
        ):
            raise _boundary_error(
                "trainer artifact verification failed",
                code="trainer_unverified",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )

    @staticmethod
    def _validate_material_identity(
        spec: TrainingJobSpec,
        materials: ResolvedTrainingPackage,
    ) -> None:
        if type(materials) is not ResolvedTrainingPackage:
            raise _boundary_error(
                "training materials must be the exact canonical resolved package",
                code="material_type",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        evidence = materials.evidence
        if not (
            hmac.compare_digest(
                materials.training_material_sha256,
                spec.training_material_sha256,
            )
            and hmac.compare_digest(
                evidence.package_manifest_sha256,
                spec.frozen_package_sha256,
            )
            and hmac.compare_digest(
                evidence.base_artifact_sha256,
                spec.base_artifact.sha256,
            )
        ):
            raise _boundary_error(
                "training material identity does not match the training job",
                code="material_identity_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
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
            raise _boundary_error(
                "step_index is outside the training job bounds",
                code="step_bounds",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if type(resume_state) is not dict:
            raise _boundary_error(
                "resume_state must be a JSON object",
                code="resume_type",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        self._validate_material_identity(spec, training_materials)

        job_fingerprint = _job_fingerprint(spec)
        trainer_state, previous_step_id = self._unwrap_resume_state(
            resume_state=resume_state,
            job_fingerprint=job_fingerprint,
            step_index=step_index,
        )
        current_step_id = _step_id(
            job_fingerprint,
            self._trainer_artifact_id,
            self._trainer_sha256,
            step_index,
        )
        request = {
            "job": _job_identity(spec),
            "job_fingerprint": job_fingerprint,
            "previous_step_id": previous_step_id,
            "protocol_version": _PROTOCOL_VERSION,
            "resume_state": trainer_state,
            "step_id": current_step_id,
            "step_index": step_index,
            "trainer_artifact": {
                "artifact_id": self._trainer_artifact_id,
                "sha256": self._trainer_sha256,
                "size_bytes": self._trainer_record.size_bytes,
            },
            "training_materials": _material_request(training_materials),
        }
        request_bytes = _canonical_json_bytes(
            request,
            max_bytes=self._max_request_bytes,
            label="training subprocess request",
            effect=TrainingWorkerFailureEffect.NO_EFFECT,
        )

        self._verify_trainer_before_effect()
        try:
            training_materials.reverify()
        except TrainingMaterialResolutionError as exc:
            raise _boundary_error(
                "training material verification failed",
                code="material_verification_failed",
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
                "trainer_artifact_id": self._trainer_artifact_id,
                "trainer_sha256": self._trainer_sha256,
                "trainer_state": trainer_resume_state,
            }
        }
        _canonical_json_bytes(
            wrapped_resume_state,
            max_bytes=self._max_request_bytes,
            label="training resume state",
            effect=TrainingWorkerFailureEffect.UNKNOWN,
        )
        try:
            return TrainingStepResult(
                resume_state=wrapped_resume_state,
                completed=completed,
                candidate_sha256=candidate_sha256,
            )
        except ValueError as exc:
            raise _boundary_error(
                "training subprocess returned invalid result evidence",
                code="invalid_result",
                effect=TrainingWorkerFailureEffect.UNKNOWN,
            ) from exc

    def _unwrap_resume_state(
        self,
        *,
        resume_state: dict[str, object],
        job_fingerprint: str,
        step_index: int,
    ) -> tuple[dict[str, object], str | None]:
        _canonical_json_bytes(
            resume_state,
            max_bytes=self._max_request_bytes,
            label="training resume state",
            effect=TrainingWorkerFailureEffect.NO_EFFECT,
        )
        if step_index == 0:
            if resume_state:
                raise _boundary_error(
                    "initial training step received unexpected resume state",
                    code="unexpected_resume",
                    effect=TrainingWorkerFailureEffect.NO_EFFECT,
                )
            return {}, None

        if set(resume_state) != {_RESUME_ENVELOPE_KEY}:
            raise _boundary_error(
                "training resume state has an invalid adapter envelope",
                code="resume_envelope",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        envelope = resume_state[_RESUME_ENVELOPE_KEY]
        if type(envelope) is not dict:
            raise _boundary_error(
                "training resume state has an invalid adapter envelope",
                code="resume_envelope",
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
            raise _boundary_error(
                "training resume state has an invalid adapter envelope",
                code="resume_envelope",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if (
            type(envelope["protocol_version"]) is not int
            or envelope["protocol_version"] != _PROTOCOL_VERSION
        ):
            raise _boundary_error(
                "training resume state uses an unsupported protocol",
                code="resume_protocol",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if envelope["job_fingerprint"] != job_fingerprint:
            raise _boundary_error(
                "training resume state does not match the current job",
                code="resume_job_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        if (
            envelope["trainer_artifact_id"] != self._trainer_artifact_id
            or envelope["trainer_sha256"] != self._trainer_sha256
        ):
            raise _boundary_error(
                "training resume state does not match the trainer artifact",
                code="resume_trainer_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )

        expected_previous_step_id = _step_id(
            job_fingerprint,
            self._trainer_artifact_id,
            self._trainer_sha256,
            step_index - 1,
        )
        if envelope["last_step_id"] != expected_previous_step_id:
            raise _boundary_error(
                "training resume state does not match the previous step",
                code="resume_step_mismatch",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            )
        trainer_state = envelope["trainer_state"]
        if type(trainer_state) is not dict:
            raise _boundary_error(
                "trainer resume state must be a JSON object",
                code="trainer_resume_type",
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
            raise _boundary_error(
                "training subprocess executable was not found",
                code="process_not_found",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc
        except PermissionError as exc:
            raise _boundary_error(
                "training subprocess executable is not executable",
                code="process_not_executable",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc
        except OSError as exc:
            raise _boundary_error(
                "training subprocess could not be started",
                code="process_start_failed",
                effect=TrainingWorkerFailureEffect.NO_EFFECT,
            ) from exc

        effect = TrainingWorkerFailureEffect.UNKNOWN
        if process.stdin is None or process.stdout is None:
            self._kill_process(process)
            raise _boundary_error(
                "training subprocess streams are unavailable",
                code="process_streams_unavailable",
                effect=effect,
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

        reader = threading.Thread(
            target=read_stdout,
            name="nika-training-stdout",
            daemon=True,
        )
        writer = threading.Thread(
            target=write_stdin,
            name="nika-training-stdin",
            daemon=True,
        )
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
            raise _boundary_error(
                "training subprocess timed out",
                code="process_timeout",
                effect=effect,
            ) from timed_out
        if reader.is_alive() or writer.is_alive():
            self._kill_process(process)
            raise _boundary_error(
                "training subprocess streams did not close",
                code="process_stream_hang",
                effect=effect,
            )
        if overflow.is_set():
            raise _boundary_error(
                "training subprocess response exceeds the byte limit",
                code="response_byte_limit",
                effect=effect,
            )
        if read_failed.is_set():
            raise _boundary_error(
                "training subprocess response could not be read",
                code="response_read_failed",
                effect=effect,
            )
        if returncode != 0:
            raise _boundary_error(
                f"training subprocess exited unsuccessfully ({returncode})",
                code="process_nonzero_exit",
                effect=effect,
            )
        if write_failed.is_set():
            raise _boundary_error(
                "training subprocess did not accept the request",
                code="request_write_failed",
                effect=effect,
            )
        return bytes(captured)

    def _parse_response(
        self,
        raw_response: bytes,
        *,
        expected_step_id: str,
    ) -> dict[str, object]:
        effect = TrainingWorkerFailureEffect.UNKNOWN
        try:
            text = raw_response.decode("utf-8", errors="strict")
            response = json.loads(
                text,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonstandard_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _boundary_error(
                "training subprocess returned an invalid JSON response",
                code="response_invalid_json",
                effect=effect,
            ) from exc

        if type(response) is not dict:
            raise _boundary_error(
                "training subprocess response must be a JSON object",
                code="response_type",
                effect=effect,
            )
        expected_keys = {
            "candidate_sha256",
            "completed",
            "protocol_version",
            "resume_state",
            "step_id",
        }
        if set(response) != expected_keys:
            raise _boundary_error(
                "training subprocess response has unexpected fields",
                code="response_fields",
                effect=effect,
            )
        if (
            type(response["protocol_version"]) is not int
            or response["protocol_version"] != _PROTOCOL_VERSION
        ):
            raise _boundary_error(
                "training subprocess uses an unsupported protocol",
                code="response_protocol",
                effect=effect,
            )
        if (
            type(response["step_id"]) is not str
            or response["step_id"] != expected_step_id
        ):
            raise _boundary_error(
                "training subprocess response has the wrong step identity",
                code="response_step_mismatch",
                effect=effect,
            )
        if type(response["completed"]) is not bool:
            raise _boundary_error(
                "training subprocess completed flag must be a boolean",
                code="response_completed_type",
                effect=effect,
            )
        if type(response["resume_state"]) is not dict:
            raise _boundary_error(
                "training subprocess resume_state must be a JSON object",
                code="response_resume_type",
                effect=effect,
            )

        candidate_sha256 = response["candidate_sha256"]
        if candidate_sha256 is not None and type(candidate_sha256) is not str:
            raise _boundary_error(
                "training subprocess candidate digest has an invalid type",
                code="response_candidate_type",
                effect=effect,
            )
        if response["completed"] is False and candidate_sha256 is not None:
            raise _boundary_error(
                "incomplete training must not publish a candidate digest",
                code="response_candidate_early",
                effect=effect,
            )

        _canonical_json_bytes(
            response["resume_state"],
            max_bytes=max(1024, self._max_request_bytes // 2),
            label="trainer resume state",
            effect=effect,
        )
        try:
            TrainingStepResult(
                resume_state=response["resume_state"],
                completed=response["completed"],
                candidate_sha256=candidate_sha256,
            )
        except ValueError as exc:
            raise _boundary_error(
                "training subprocess returned invalid result evidence",
                code="invalid_result",
                effect=effect,
            ) from exc
        return response
