from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from nika_core.artifacts import (
    ArtifactLocationKind,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactRegistryError,
    ArtifactVerification,
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
_TRAINER_ARTIFACT_KIND = "training_worker_executable"
_RESUME_ENVELOPE_KEY = "_nika_subprocess"
_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_TIMEOUT_SECONDS = 3600.0
_MAX_ARGUMENT_BYTES = 4096
_MAX_ARGUMENTS_BYTES = 32 * 1024
_MAX_ENVIRONMENT_ENTRIES = 128
_MAX_ENVIRONMENT_FIELD_BYTES = 16 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 4096
_STREAM_JOIN_TIMEOUT_SECONDS = 1.0
_READ_CHUNK_BYTES = 64 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")


class TrainingSubprocessError(ValueError):
    """Configuration error at the subprocess-adapter boundary."""


def _canonical_json_bytes(value: object, *, max_bytes: int, label: str) -> bytes:
    _validate_json_tree(value, label=label)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TrainingSubprocessError(f"{label} is not valid canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise TrainingSubprocessError(f"{label} exceeds the configured byte limit")
    return encoded


def _validate_json_tree(value: object, *, label: str) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise TrainingSubprocessError(f"{label} contains too many JSON values")
        if depth > _MAX_JSON_DEPTH:
            raise TrainingSubprocessError(f"{label} exceeds the JSON nesting limit")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise TrainingSubprocessError(f"{label} contains a non-finite number")
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise TrainingSubprocessError(f"{label} contains a non-string JSON key")
                visit(child, depth + 1)
            return
        raise TrainingSubprocessError(f"{label} contains a non-JSON value")

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


def _validate_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes)):
        raise TypeError("arguments must be a sequence of strings")
    normalized = tuple(arguments)
    total_bytes = 0
    for argument in normalized:
        if type(argument) is not str or "\x00" in argument:
            raise ValueError("training arguments must be NUL-free strings")
        try:
            encoded_length = len(argument.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("training arguments must be valid UTF-8 text") from exc
        if encoded_length > _MAX_ARGUMENT_BYTES:
            raise ValueError("training argument exceeds the configured byte limit")
        total_bytes += encoded_length
    if total_bytes > _MAX_ARGUMENTS_BYTES:
        raise ValueError("training arguments exceed the configured byte limit")
    return normalized


def _validate_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return {}
    if type(environment) is not dict:
        raise TypeError("environment must be an exact dict when provided")
    if len(environment) > _MAX_ENVIRONMENT_ENTRIES:
        raise ValueError("environment contains too many entries")

    result: dict[str, str] = {}
    for key, value in environment.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError("environment keys and values must be strings")
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError("environment contains an invalid key or value")
        try:
            key_bytes = len(key.encode("utf-8"))
            value_bytes = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("environment must be valid UTF-8 text") from exc
        if key_bytes > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment key exceeds the configured byte limit")
        if value_bytes > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment value exceeds the configured byte limit")
        result[key] = value
    return result


def _validate_durable_resume_tree(value: object) -> None:
    """Keep trainer resume state bounded, machine-oriented and free of direct path strings."""
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise TrainingSubprocessError("trainer resume state contains too many values")
        if depth > _MAX_JSON_DEPTH:
            raise TrainingSubprocessError("trainer resume state exceeds the nesting limit")
        if item is None or type(item) in (bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise TrainingSubprocessError("trainer resume state contains a non-finite number")
            return
        if type(item) is str:
            if "/" in item or "\\" in item or "\x00" in item:
                raise TrainingSubprocessError(
                    "trainer resume state must not contain filesystem paths"
                )
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise TrainingSubprocessError(
                    "trainer resume state contains control characters"
                )
            if len(item.encode("utf-8")) > _MAX_ENVIRONMENT_FIELD_BYTES:
                raise TrainingSubprocessError(
                    "trainer resume state string exceeds the configured byte limit"
                )
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise TrainingSubprocessError(
                        "trainer resume state contains a non-string JSON key"
                    )
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise TrainingSubprocessError("trainer resume state contains a non-JSON value")

    visit(value, 0)


def _arguments_sha256(arguments: tuple[str, ...]) -> str:
    payload = _canonical_json_bytes(
        list(arguments),
        max_bytes=_DEFAULT_MAX_REQUEST_BYTES,
        label="training arguments",
    )
    return hashlib.sha256(b"nika-training-arguments-v1\x00" + payload).hexdigest()


def _job_identity(
    spec: TrainingJobSpec,
    *,
    trainer_artifact_id: str,
    trainer_sha256: str,
    arguments_sha256: str,
) -> dict[str, object]:
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
        "trainer": {
            "arguments_sha256": arguments_sha256,
            "artifact_id": trainer_artifact_id,
            "sha256": trainer_sha256,
        },
        "training_material_sha256": spec.training_material_sha256,
    }


def _job_fingerprint(
    spec: TrainingJobSpec,
    *,
    trainer_artifact_id: str,
    trainer_sha256: str,
    arguments_sha256: str,
) -> str:
    identity = _canonical_json_bytes(
        _job_identity(
            spec,
            trainer_artifact_id=trainer_artifact_id,
            trainer_sha256=trainer_sha256,
            arguments_sha256=arguments_sha256,
        ),
        max_bytes=_DEFAULT_MAX_REQUEST_BYTES,
        label="training job identity",
    )
    return hashlib.sha256(b"nika-training-job-v2\x00" + identity).hexdigest()


def _step_id(job_fingerprint: str, step_index: int) -> str:
    material = f"nika-training-step-v2\x00{job_fingerprint}\x00{step_index}".encode()
    return hashlib.sha256(material).hexdigest()


def _material_request(materials: ResolvedTrainingPackage) -> dict[str, object]:
    return {
        "base_artifact_sha256": materials.evidence.base_artifact_sha256,
        "items": [
            {
                "byte_count": item.evidence.byte_count,
                "path": str(item.path),
                "sha256": item.evidence.artifact_sha256,
                "split": item.evidence.split.value,
            }
            for item in materials.materials
        ],
        "package_manifest_sha256": materials.evidence.package_manifest_sha256,
        "training_material_sha256": materials.training_material_sha256,
    }


def _no_effect(code: str) -> TrainingWorkerError:
    return TrainingWorkerError(code, effect=TrainingWorkerFailureEffect.NO_EFFECT)


def _unknown_effect(code: str) -> TrainingWorkerError:
    return TrainingWorkerError(code, effect=TrainingWorkerFailureEffect.UNKNOWN)


class SubprocessTrainingWorker:
    """Shell-free worker bound to one Artifact-Registry executable.

    Durable resume state binds only immutable identities, never transient training-material paths.
    Before each subprocess effect the adapter verifies the registered executable and the resolved
    material bytes again. Once a child process has been created, any transport/protocol failure is
    reported as UNKNOWN because the external trainer may already have caused an effect.
    """

    def __init__(
        self,
        *,
        artifact_registry: ArtifactRegistry,
        trainer_artifact_id: str,
        arguments: Sequence[str] = (),
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if type(artifact_registry) is not ArtifactRegistry:
            raise TypeError("artifact_registry must be the canonical ArtifactRegistry")
        self._artifact_registry = artifact_registry
        self._trainer_artifact_id = _validate_sha256(
            trainer_artifact_id,
            name="trainer_artifact_id",
        )
        self._arguments = _validate_arguments(arguments)
        self._arguments_sha256 = _arguments_sha256(self._arguments)
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

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        if type(spec) is not TrainingJobSpec:
            raise _no_effect("invalid_job_spec")
        if type(training_materials) is not ResolvedTrainingPackage:
            raise _no_effect("invalid_training_materials")
        if type(step_index) is not int or step_index < 0 or step_index >= spec.max_steps:
            raise _no_effect("invalid_step_index")
        if type(resume_state) is not dict:
            raise _no_effect("invalid_resume_state")

        self._require_material_identity(spec, training_materials)
        trainer = self._verified_trainer_record()
        trainer_sha256 = trainer.sha256
        job_fingerprint = self._job_fingerprint(spec, trainer_sha256)

        try:
            trainer_state, previous_step_id = self._unwrap_resume_state(
                resume_state=resume_state,
                job_fingerprint=job_fingerprint,
                trainer_sha256=trainer_sha256,
                step_index=step_index,
            )
            current_step_id = _step_id(job_fingerprint, step_index)
            request = {
                "job": _job_identity(
                    spec,
                    trainer_artifact_id=self._trainer_artifact_id,
                    trainer_sha256=trainer_sha256,
                    arguments_sha256=self._arguments_sha256,
                ),
                "job_fingerprint": job_fingerprint,
                "materials": _material_request(training_materials),
                "previous_step_id": previous_step_id,
                "protocol_version": _PROTOCOL_VERSION,
                "resume_state": trainer_state,
                "step_id": current_step_id,
                "step_index": step_index,
                "trainer": {
                    "arguments_sha256": self._arguments_sha256,
                    "artifact_id": self._trainer_artifact_id,
                    "sha256": trainer_sha256,
                    "size_bytes": trainer.size_bytes,
                },
            }
            request_bytes = _canonical_json_bytes(
                request,
                max_bytes=self._max_request_bytes,
                label="training subprocess request",
            )
            training_materials.reverify()
        except TrainingWorkerError:
            raise
        except (TrainingSubprocessError, TrainingMaterialResolutionError) as exc:
            raise _no_effect("preflight_rejected") from exc

        stdout = self._execute(
            command=(trainer.locator, *self._arguments),
            request_bytes=request_bytes + b"\n",
            expected_trainer=trainer,
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
                "arguments_sha256": self._arguments_sha256,
                "job_fingerprint": job_fingerprint,
                "last_step_id": current_step_id,
                "protocol_version": _PROTOCOL_VERSION,
                "trainer_artifact_id": self._trainer_artifact_id,
                "trainer_sha256": trainer_sha256,
                "trainer_state": trainer_resume_state,
            }
        }
        try:
            _canonical_json_bytes(
                wrapped_resume_state,
                max_bytes=self._max_request_bytes,
                label="training resume state",
            )
            return TrainingStepResult(
                resume_state=wrapped_resume_state,
                completed=completed,
                candidate_sha256=candidate_sha256,
            )
        except (TrainingSubprocessError, ValueError) as exc:
            raise _unknown_effect("invalid_result") from exc

    def _job_fingerprint(self, spec: TrainingJobSpec, trainer_sha256: str) -> str:
        try:
            return _job_fingerprint(
                spec,
                trainer_artifact_id=self._trainer_artifact_id,
                trainer_sha256=trainer_sha256,
                arguments_sha256=self._arguments_sha256,
            )
        except TrainingSubprocessError as exc:
            raise _no_effect("invalid_job_identity") from exc

    @staticmethod
    def _require_material_identity(
        spec: TrainingJobSpec,
        materials: ResolvedTrainingPackage,
    ) -> None:
        try:
            matches = (
                hmac.compare_digest(
                    materials.training_material_sha256,
                    spec.training_material_sha256,
                )
                and hmac.compare_digest(
                    materials.evidence.package_manifest_sha256,
                    spec.frozen_package_sha256,
                )
                and hmac.compare_digest(
                    materials.evidence.base_artifact_sha256,
                    spec.base_artifact.sha256,
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise _no_effect("training_material_identity_invalid") from exc
        if not matches:
            raise _no_effect("training_material_identity_mismatch")

    def _verified_trainer_record(self) -> ArtifactRecord:
        try:
            record = self._artifact_registry.get(self._trainer_artifact_id)
            if type(record) is not ArtifactRecord:
                raise ArtifactRegistryError("trainer registry returned a non-canonical record")
            if record.location_kind is not ArtifactLocationKind.LOCAL_FILE:
                raise ArtifactRegistryError("trainer artifact is not a local file")
            if record.kind != _TRAINER_ARTIFACT_KIND:
                raise ArtifactRegistryError("trainer artifact has the wrong kind")
            path = Path(record.locator)
            if not path.is_absolute():
                raise ArtifactRegistryError("trainer artifact path is not absolute")
            verification = self._artifact_registry.verify(self._trainer_artifact_id)
            self._require_verified_artifact(record, verification)
            return record
        except (ArtifactRegistryError, KeyError, OSError, ValueError) as exc:
            raise _no_effect("trainer_artifact_unavailable") from exc

    @staticmethod
    def _require_verified_artifact(
        record: ArtifactRecord,
        verification: ArtifactVerification,
    ) -> None:
        if type(verification) is not ArtifactVerification:
            raise ArtifactRegistryError("trainer verification returned non-canonical evidence")
        if verification.state is not ArtifactVerificationState.VERIFIED:
            raise ArtifactRegistryError("trainer artifact is not verified")
        if verification.artifact_id != record.artifact_id:
            raise ArtifactRegistryError("trainer verification identity mismatch")
        if verification.actual_sha256 != record.sha256:
            raise ArtifactRegistryError("trainer verification digest mismatch")
        if verification.actual_size_bytes != record.size_bytes:
            raise ArtifactRegistryError("trainer verification size mismatch")

    def _post_spawn_verify(self, expected: ArtifactRecord) -> None:
        try:
            current = self._artifact_registry.get(self._trainer_artifact_id)
            if type(current) is not ArtifactRecord:
                raise ArtifactRegistryError("trainer registry returned a non-canonical record")
            if current != expected:
                raise ArtifactRegistryError("trainer registry identity changed during spawn")
            verification = self._artifact_registry.verify(self._trainer_artifact_id)
            self._require_verified_artifact(expected, verification)
        except (ArtifactRegistryError, KeyError, OSError, ValueError) as exc:
            raise _unknown_effect("trainer_identity_changed") from exc

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
            label="training resume state",
        )
        if step_index == 0:
            if resume_state:
                raise TrainingSubprocessError(
                    "initial training step received unexpected resume state"
                )
            return {}, None

        if set(resume_state) != {_RESUME_ENVELOPE_KEY}:
            raise TrainingSubprocessError("training resume state has an invalid adapter envelope")
        envelope = resume_state[_RESUME_ENVELOPE_KEY]
        if type(envelope) is not dict:
            raise TrainingSubprocessError("training resume state has an invalid adapter envelope")
        expected_keys = {
            "arguments_sha256",
            "job_fingerprint",
            "last_step_id",
            "protocol_version",
            "trainer_artifact_id",
            "trainer_sha256",
            "trainer_state",
        }
        if set(envelope) != expected_keys:
            raise TrainingSubprocessError("training resume state has an invalid adapter envelope")
        if type(envelope["protocol_version"]) is not int or (
            envelope["protocol_version"] != _PROTOCOL_VERSION
        ):
            raise TrainingSubprocessError("training resume state uses an unsupported protocol")
        if envelope["job_fingerprint"] != job_fingerprint:
            raise TrainingSubprocessError("training resume state does not match the current job")
        if envelope["trainer_artifact_id"] != self._trainer_artifact_id:
            raise TrainingSubprocessError(
                "training resume state does not match the trainer artifact"
            )
        if envelope["trainer_sha256"] != trainer_sha256:
            raise TrainingSubprocessError(
                "training resume state does not match the trainer artifact"
            )
        if envelope["arguments_sha256"] != self._arguments_sha256:
            raise TrainingSubprocessError(
                "training resume state does not match trainer arguments"
            )

        expected_previous_step_id = _step_id(job_fingerprint, step_index - 1)
        if envelope["last_step_id"] != expected_previous_step_id:
            raise TrainingSubprocessError(
                "training resume state does not match the previous step"
            )
        trainer_state = envelope["trainer_state"]
        if type(trainer_state) is not dict:
            raise TrainingSubprocessError("trainer resume state must be a JSON object")
        _validate_durable_resume_tree(trainer_state)
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

    @classmethod
    def _terminate_and_reap(cls, process: subprocess.Popen[bytes]) -> None:
        cls._kill_process(process)
        try:
            process.wait(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return

    def _execute(
        self,
        *,
        command: tuple[str, ...],
        request_bytes: bytes,
        expected_trainer: ArtifactRecord,
    ) -> bytes:
        try:
            process = subprocess.Popen(
                command,
                env=dict(self._environment),
                shell=False,
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise _no_effect("spawn_failed") from exc

        try:
            self._post_spawn_verify(expected_trainer)
        except TrainingWorkerError:
            self._terminate_and_reap(process)
            raise

        if process.stdin is None or process.stdout is None:
            self._terminate_and_reap(process)
            raise _unknown_effect("stream_setup_failed")

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

        timed_out = False
        try:
            returncode = process.wait(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
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

        if timed_out:
            raise _unknown_effect("timeout")
        if reader.is_alive() or writer.is_alive():
            self._kill_process(process)
            raise _unknown_effect("stream_shutdown_failed")
        if overflow.is_set():
            raise _unknown_effect("response_too_large")
        if read_failed.is_set():
            raise _unknown_effect("response_read_failed")
        if returncode != 0:
            raise _unknown_effect("process_failed")
        if write_failed.is_set():
            raise _unknown_effect("request_write_failed")
        return bytes(captured)

    def _parse_response(
        self,
        raw_response: bytes,
        *,
        expected_step_id: str,
    ) -> dict[str, object]:
        try:
            text = raw_response.decode("utf-8", errors="strict")
            response = json.loads(
                text,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonstandard_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _unknown_effect("invalid_response") from None

        if type(response) is not dict:
            raise _unknown_effect("invalid_response")
        expected_keys = {
            "candidate_sha256",
            "completed",
            "protocol_version",
            "resume_state",
            "step_id",
        }
        if set(response) != expected_keys:
            raise _unknown_effect("invalid_response_fields")
        if type(response["protocol_version"]) is not int or (
            response["protocol_version"] != _PROTOCOL_VERSION
        ):
            raise _unknown_effect("unsupported_protocol")
        if type(response["step_id"]) is not str or response["step_id"] != expected_step_id:
            raise _unknown_effect("step_identity_mismatch")
        if type(response["completed"]) is not bool:
            raise _unknown_effect("invalid_completed_flag")
        if type(response["resume_state"]) is not dict:
            raise _unknown_effect("invalid_resume_state")

        candidate_sha256 = response["candidate_sha256"]
        if candidate_sha256 is not None and type(candidate_sha256) is not str:
            raise _unknown_effect("invalid_candidate_digest")
        if response["completed"] is False and candidate_sha256 is not None:
            raise _unknown_effect("invalid_candidate_state")
        try:
            _validate_durable_resume_tree(response["resume_state"])
            _canonical_json_bytes(
                response["resume_state"],
                max_bytes=max(1024, self._max_request_bytes // 2),
                label="trainer resume state",
            )
            TrainingStepResult(
                resume_state=response["resume_state"],
                completed=response["completed"],
                candidate_sha256=candidate_sha256,
            )
        except (TrainingSubprocessError, ValueError):
            raise _unknown_effect("invalid_result") from None
        return response
