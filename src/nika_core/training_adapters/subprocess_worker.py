from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from typing import NoReturn

from nika_core.training_runtime import TrainingJobSpec, TrainingStepResult

_PROTOCOL_VERSION = 1
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


class TrainingSubprocessError(RuntimeError):
    """Safe public failure from the external training-process boundary."""


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
        "job_id": spec.job_id,
        "max_steps": spec.max_steps,
        "owner_id": spec.owner_id,
        "project_id": spec.project_id,
        "resource_scope": spec.resource_scope,
        "task_id": spec.task_id,
    }


def _job_fingerprint(spec: TrainingJobSpec) -> str:
    identity = _canonical_json_bytes(
        _job_identity(spec),
        max_bytes=_DEFAULT_MAX_REQUEST_BYTES,
        label="training job identity",
    )
    return hashlib.sha256(b"nika-training-job-v1\x00" + identity).hexdigest()


def _step_id(job_fingerprint: str, step_index: int) -> str:
    material = f"nika-training-step-v1\x00{job_fingerprint}\x00{step_index}".encode()
    return hashlib.sha256(material).hexdigest()


class SubprocessTrainingWorker:
    """Shell-free TrainingWorkerPort adapter for an explicitly configured local trainer.

    The subprocess receives one canonical JSON request on stdin and must return one
    strict JSON response on stdout. The adapter does not inherit the parent process
    environment by default and never includes raw subprocess output in raised errors.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._command = _validate_command(command)
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
    ) -> TrainingStepResult:
        if type(step_index) is not int or step_index < 0 or step_index >= spec.max_steps:
            raise TrainingSubprocessError("step_index is outside the training job bounds")
        if type(resume_state) is not dict:
            raise TrainingSubprocessError("resume_state must be a JSON object")

        job_fingerprint = _job_fingerprint(spec)
        trainer_state, previous_step_id = self._unwrap_resume_state(
            resume_state=resume_state,
            job_fingerprint=job_fingerprint,
            step_index=step_index,
        )
        current_step_id = _step_id(job_fingerprint, step_index)
        request = {
            "job": _job_identity(spec),
            "job_fingerprint": job_fingerprint,
            "previous_step_id": previous_step_id,
            "protocol_version": _PROTOCOL_VERSION,
            "resume_state": trainer_state,
            "step_id": current_step_id,
            "step_index": step_index,
        }
        request_bytes = _canonical_json_bytes(
            request,
            max_bytes=self._max_request_bytes,
            label="training subprocess request",
        )

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
                "trainer_state": trainer_resume_state,
            }
        }
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
        )
        if step_index == 0:
            if resume_state:
                raise TrainingSubprocessError("initial training step received unexpected resume state")
            return {}, None

        if set(resume_state) != {_RESUME_ENVELOPE_KEY}:
            raise TrainingSubprocessError("training resume state has an invalid adapter envelope")
        envelope = resume_state[_RESUME_ENVELOPE_KEY]
        if type(envelope) is not dict:
            raise TrainingSubprocessError("training resume state has an invalid adapter envelope")
        expected_keys = {
            "job_fingerprint",
            "last_step_id",
            "protocol_version",
            "trainer_state",
        }
        if set(envelope) != expected_keys:
            raise TrainingSubprocessError("training resume state has an invalid adapter envelope")
        if envelope["protocol_version"] != _PROTOCOL_VERSION or type(
            envelope["protocol_version"]
        ) is not int:
            raise TrainingSubprocessError("training resume state uses an unsupported protocol")
        if envelope["job_fingerprint"] != job_fingerprint:
            raise TrainingSubprocessError("training resume state does not match the current job")

        expected_previous_step_id = _step_id(job_fingerprint, step_index - 1)
        if envelope["last_step_id"] != expected_previous_step_id:
            raise TrainingSubprocessError("training resume state does not match the previous step")
        trainer_state = envelope["trainer_state"]
        if type(trainer_state) is not dict:
            raise TrainingSubprocessError("trainer resume state must be a JSON object")
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
            raise TrainingSubprocessError("training subprocess executable was not found") from exc
        except PermissionError as exc:
            raise TrainingSubprocessError("training subprocess executable is not executable") from exc
        except OSError as exc:
            raise TrainingSubprocessError("training subprocess could not be started") from exc

        if process.stdin is None or process.stdout is None:
            self._kill_process(process)
            raise TrainingSubprocessError("training subprocess streams are unavailable")

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
            raise TrainingSubprocessError("training subprocess timed out") from timed_out
        if reader.is_alive() or writer.is_alive():
            self._kill_process(process)
            raise TrainingSubprocessError("training subprocess streams did not close")
        if overflow.is_set():
            raise TrainingSubprocessError("training subprocess response exceeds the byte limit")
        if read_failed.is_set():
            raise TrainingSubprocessError("training subprocess response could not be read")
        if returncode != 0:
            raise TrainingSubprocessError(
                f"training subprocess exited unsuccessfully ({returncode})"
            )
        if write_failed.is_set():
            raise TrainingSubprocessError("training subprocess did not accept the request")
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
            raise TrainingSubprocessError("training subprocess returned an invalid JSON response") from exc

        if type(response) is not dict:
            raise TrainingSubprocessError("training subprocess response must be a JSON object")
        expected_keys = {
            "candidate_sha256",
            "completed",
            "protocol_version",
            "resume_state",
            "step_id",
        }
        if set(response) != expected_keys:
            raise TrainingSubprocessError("training subprocess response has unexpected fields")
        if type(response["protocol_version"]) is not int or response["protocol_version"] != 1:
            raise TrainingSubprocessError("training subprocess uses an unsupported protocol")
        if type(response["step_id"]) is not str or response["step_id"] != expected_step_id:
            raise TrainingSubprocessError("training subprocess response has the wrong step identity")
        if type(response["completed"]) is not bool:
            raise TrainingSubprocessError("training subprocess completed flag must be a boolean")
        if type(response["resume_state"]) is not dict:
            raise TrainingSubprocessError("training subprocess resume_state must be a JSON object")

        candidate_sha256 = response["candidate_sha256"]
        if candidate_sha256 is not None and type(candidate_sha256) is not str:
            raise TrainingSubprocessError("training subprocess candidate digest has an invalid type")
        if response["completed"] is False and candidate_sha256 is not None:
            raise TrainingSubprocessError("incomplete training must not publish a candidate digest")

        _canonical_json_bytes(
            response["resume_state"],
            max_bytes=max(1024, self._max_request_bytes // 2),
            label="trainer resume state",
        )
        try:
            TrainingStepResult(
                resume_state=response["resume_state"],
                completed=response["completed"],
                candidate_sha256=candidate_sha256,
            )
        except ValueError as exc:
            raise TrainingSubprocessError("training subprocess returned invalid result evidence") from exc
        return response
