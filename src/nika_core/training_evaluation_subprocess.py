from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import NoReturn

from nika_core.artifacts import (
    ArtifactLocationKind,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactRegistryError,
    ArtifactVerificationState,
)
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactRegistryError,
    ModelIntegrityBasis,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderKind,
)
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    verify_candidate_artifact,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_PROTOCOL_VERSION = 1
_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_TIMEOUT_SECONDS = 3600.0
_MAX_ARGUMENT_BYTES = 4096
_MAX_COMMAND_BYTES = 32 * 1024
_MAX_ENVIRONMENT_ENTRIES = 128
_MAX_ENVIRONMENT_FIELD_BYTES = 16 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 4096
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
_SECRET_ENVIRONMENT_MARKERS = (
    "api_key",
    "apikey",
    "auth",
    "credential",
    "passwd",
    "password",
    "secret",
    "token",
)
_EVALUATION_METADATA_KEYS = frozenset(
    {
        "benchmark_configuration_sha256",
        "benchmark_execution_config_sha256",
        "benchmark_run_id",
        "evaluation_case_id",
        "evaluation_set_id",
        "evaluation_set_sha256",
        "evaluation_set_version",
        "model_candidate_id",
    }
)


def _error(
    code: ModelErrorCode,
    message: str,
    *,
    provider_id: str | None,
    effect: ModelFailureEffect,
) -> ModelGatewayError:
    return ModelGatewayError(
        code,
        message,
        provider_id=provider_id,
        failure_effect=effect,
    )


def _validate_sha256(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
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


def _validate_byte_limit(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1024 or value > 1024 * 1024:
        raise ValueError(f"{name} must be an integer from 1024 through 1048576")
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
            raise ValueError("evaluation executable must use an absolute path")
    if total_bytes > _MAX_COMMAND_BYTES:
        raise ValueError("command exceeds the configured byte limit")
    return normalized


def _validate_command_artifact_ids(
    command: tuple[str, ...],
    *,
    evaluator_artifact_id: str,
    command_artifact_ids: Mapping[int, str] | None,
) -> dict[int, str]:
    result = {
        0: _validate_sha256(evaluator_artifact_id, name="evaluator_artifact_id"),
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
                "use stdin for evaluation data"
            )
    return result


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
        normalized_key = key.casefold()
        if (
            normalized_key in _FORBIDDEN_ENVIRONMENT_KEYS
            or normalized_key.startswith("python")
            or normalized_key.startswith("ld_")
            or normalized_key.startswith("dyld_")
        ):
            raise ValueError("environment may not alter runtime or loader authority")
        if any(marker in normalized_key for marker in _SECRET_ENVIRONMENT_MARKERS):
            raise ValueError("evaluation environment must not contain credential material")
        if len(key.encode("utf-8")) > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment key exceeds the configured byte limit")
        if len(value.encode("utf-8")) > _MAX_ENVIRONMENT_FIELD_BYTES:
            raise ValueError("environment value exceeds the configured byte limit")
        result[key] = value
    return result


def _validate_json_tree(value: object, *, label: str) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError(f"{label} contains too many values")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{label} is too deeply nested")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{label} contains a non-finite number")
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError(f"{label} contains a non-string key")
                visit(child, depth + 1)
            return
        raise ValueError(f"{label} contains a non-JSON value")

    visit(value, 0)


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
        raise ValueError(f"{label} is not canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the configured byte limit")
    return encoded


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _snapshot_request(request: ModelRequest) -> ModelRequest:
    if type(request) is not ModelRequest:
        raise _error(
            ModelErrorCode.INVALID_REQUEST,
            "subprocess evaluation request is invalid",
            provider_id=None,
            effect=ModelFailureEffect.NO_EFFECT,
        )
    try:
        return ModelRequest(
            request_id=request.request_id,
            messages=request.messages,
            model=request.model,
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            fallback_provider_ids=request.fallback_provider_ids,
            privacy=request.privacy,
            timeout_seconds=request.timeout_seconds,
            temperature=request.temperature,
            metadata=dict(request.metadata),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error(
            ModelErrorCode.INVALID_REQUEST,
            "subprocess evaluation request is invalid",
            provider_id=None,
            effect=ModelFailureEffect.NO_EFFECT,
        ) from exc


def _snapshot_binding(binding: TrainingEvaluationBinding) -> TrainingEvaluationBinding:
    try:
        return binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error(
            ModelErrorCode.INVALID_REQUEST,
            "training evaluation binding is invalid",
            provider_id=None,
            effect=ModelFailureEffect.NO_EFFECT,
        ) from exc


def _snapshot_descriptor(descriptor: ModelArtifactDescriptor) -> ModelArtifactDescriptor:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be an exact ModelArtifactDescriptor")
    try:
        return ModelArtifactDescriptor.from_json(descriptor.canonical_json())
    except (AttributeError, ModelArtifactRegistryError, TypeError, ValueError) as exc:
        raise ValueError("descriptor must be canonical") from exc


def _usage_value(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or null")
    return value


class RegistrySubprocessLoadedModelAttestor:
    """Registry-authorized local evaluator producing same-effect loaded-byte evidence.

    The external command is the trusted attestor implementation. Every executable or
    absolute command-file argument is bound through Artifact Registry and reverified
    immediately before process creation. The candidate is independently verified
    against its canonical ModelArtifactDescriptor before launch; the subprocess must
    then report the digest and size it actually loaded in the same invocation that
    returns the model response.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        artifact_registry: ArtifactRegistry,
        evaluator_artifact_id: str,
        candidate_path: str,
        descriptor: ModelArtifactDescriptor,
        allowed_root: str | None = None,
        command_artifact_ids: Mapping[int, str] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._command = _validate_command(command)
        if type(artifact_registry) is not ArtifactRegistry:
            raise TypeError("artifact_registry must be the canonical ArtifactRegistry")
        if type(candidate_path) is not str or not os.path.isabs(candidate_path):
            raise ValueError("candidate_path must be an absolute text path")
        if allowed_root is not None and (
            type(allowed_root) is not str or not os.path.isabs(allowed_root)
        ):
            raise ValueError("allowed_root must be an absolute text path or null")

        self._artifact_registry = artifact_registry
        self._command_artifact_ids = _validate_command_artifact_ids(
            self._command,
            evaluator_artifact_id=evaluator_artifact_id,
            command_artifact_ids=command_artifact_ids,
        )
        self._candidate_path = candidate_path
        self._allowed_root = allowed_root
        self._descriptor = _snapshot_descriptor(descriptor)
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._environment = _validate_environment(environment)
        self._max_request_bytes = _validate_byte_limit(
            max_request_bytes,
            name="max_request_bytes",
        )
        self._max_response_bytes = _validate_byte_limit(
            max_response_bytes,
            name="max_response_bytes",
        )

        records = self._get_command_records()
        self._command_records = records
        self._attestor_id = records[0].artifact_id
        self._attestor_sha256 = self._command_authority_sha256(records)

    @property
    def attestor_id(self) -> str:
        return self._attestor_id

    @property
    def attestor_sha256(self) -> str:
        return self._attestor_sha256

    async def complete_attested(
        self,
        request: ModelRequest,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        canonical_request = _snapshot_request(request)
        canonical_binding = _snapshot_binding(binding)
        provider_id = canonical_binding.challenger_provider_id
        self._validate_request_binding(canonical_request, canonical_binding)
        self._validate_descriptor_binding(canonical_binding)

        try:
            verified_candidate = await asyncio.to_thread(
                verify_candidate_artifact,
                self._candidate_path,
                self._descriptor,
                allowed_root=self._allowed_root,
            )
        except (CandidateArtifactIntegrityError, TypeError, ValueError) as exc:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "candidate model bytes could not be verified before evaluation",
                provider_id=provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            ) from exc

        if (
            verified_candidate.sha256 != canonical_binding.challenger_sha256
            or verified_candidate.size_bytes != canonical_binding.challenger_size_bytes
            or verified_candidate.descriptor_digest != canonical_binding.descriptor_digest
            or verified_candidate.registry_key != canonical_binding.descriptor_registry_key
        ):
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "candidate verification evidence does not match evaluation binding",
                provider_id=provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            )

        try:
            await asyncio.to_thread(
                self._verify_command_records,
                self._command_records,
            )
        except ModelGatewayError:
            raise

        payload = self._request_payload(canonical_request, canonical_binding)
        try:
            request_bytes = _canonical_json_bytes(
                payload,
                max_bytes=self._max_request_bytes,
                label="evaluation subprocess request",
            )
        except ValueError as exc:
            raise _error(
                ModelErrorCode.INVALID_REQUEST,
                "evaluation subprocess request exceeds the admitted protocol",
                provider_id=provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            ) from exc

        raw_response = await self._execute(
            request_bytes + b"\n",
            provider_id=provider_id,
            timeout_seconds=min(
                self._timeout_seconds,
                float(canonical_request.timeout_seconds),
            ),
        )
        response_data = self._parse_response(
            raw_response,
            request=canonical_request,
            binding=canonical_binding,
        )
        usage = response_data["usage"]
        assert type(usage) is dict
        model_response = ModelResponse(
            request_id=canonical_request.request_id,
            text=response_data["text"],
            provider_id=provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=canonical_binding.challenger_model_id,
            usage=ModelUsage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
            ),
        )
        attestation = LoadedModelArtifactAttestation(
            request_id=canonical_request.request_id,
            binding_sha256=canonical_binding.binding_sha256,
            provider_id=provider_id,
            model_id=canonical_binding.challenger_model_id,
            artifact_sha256=response_data["loaded_artifact_sha256"],
            descriptor_digest=response_data["descriptor_digest"],
            attestor_id=self._attestor_id,
            attestor_sha256=self._attestor_sha256,
        )
        return AttestedModelCompletionResult(
            response=model_response,
            attestation=attestation,
        )

    def _validate_request_binding(
        self,
        request: ModelRequest,
        binding: TrainingEvaluationBinding,
    ) -> None:
        if (
            request.provider_kind is not ProviderKind.LOCAL
            or request.provider_id != binding.challenger_provider_id
            or request.model != binding.challenger_model_id
            or request.fallback_provider_ids
            or request.metadata.get("model_candidate_id") != binding.challenger_candidate_id
            or request.metadata.get("evaluation_set_sha256")
            != binding.evaluation_set_sha256
            or any(key not in _EVALUATION_METADATA_KEYS for key in request.metadata)
        ):
            raise _error(
                ModelErrorCode.INVALID_REQUEST,
                "evaluation request route does not match training binding",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            )

    def _validate_descriptor_binding(self, binding: TrainingEvaluationBinding) -> None:
        descriptor = self._descriptor
        if (
            descriptor.integrity_basis is not ModelIntegrityBasis.SHA256
            or descriptor.provider_id != binding.challenger_provider_id
            or descriptor.model_id != binding.challenger_model_id
            or descriptor.sha256 != binding.challenger_sha256
            or descriptor.size_bytes != binding.challenger_size_bytes
            or descriptor.descriptor_digest != binding.descriptor_digest
            or descriptor.registry_key != binding.descriptor_registry_key
        ):
            raise _error(
                ModelErrorCode.INVALID_REQUEST,
                "candidate descriptor does not match training evaluation binding",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            )

    def _get_command_records(self) -> dict[int, ArtifactRecord]:
        records: dict[int, ArtifactRecord] = {}
        for index, artifact_id in sorted(self._command_artifact_ids.items()):
            try:
                record = self._artifact_registry.get(artifact_id)
            except (ArtifactRegistryError, ValueError) as exc:
                raise ValueError("evaluation command artifact is unavailable") from exc
            if type(record) is not ArtifactRecord:
                raise TypeError("evaluation command artifact record is invalid")
            if record.location_kind is not ArtifactLocationKind.LOCAL_FILE:
                raise ValueError("evaluation command artifacts must be local files")
            expected_kind = (
                "model_evaluator_executable"
                if index == 0
                else "model_evaluator_command_file"
            )
            if record.kind != expected_kind:
                raise ValueError("evaluation command artifact kind does not match argv role")
            if _normalized_path(self._command[index]) != _normalized_path(record.locator):
                raise ValueError("evaluation command artifact does not match argv")
            records[index] = record
        return records

    def _verify_command_records(
        self,
        expected_records: Mapping[int, ArtifactRecord],
    ) -> None:
        for expected in expected_records.values():
            try:
                verification = self._artifact_registry.verify(expected.artifact_id)
                current = self._artifact_registry.get(expected.artifact_id)
            except (ArtifactRegistryError, ValueError) as exc:
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation command artifact verification failed",
                    provider_id=self._descriptor.provider_id,
                    effect=ModelFailureEffect.NO_EFFECT,
                ) from exc
            if type(current) is not ArtifactRecord or current != expected:
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation command artifact authority changed",
                    provider_id=self._descriptor.provider_id,
                    effect=ModelFailureEffect.NO_EFFECT,
                )
            if verification.state is not ArtifactVerificationState.VERIFIED:
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation command artifact is not verified",
                    provider_id=self._descriptor.provider_id,
                    effect=ModelFailureEffect.NO_EFFECT,
                )
            if (
                verification.expected_sha256 != expected.sha256
                or verification.actual_sha256 != expected.sha256
                or verification.expected_size_bytes != expected.size_bytes
                or verification.actual_size_bytes != expected.size_bytes
            ):
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation command artifact evidence is inconsistent",
                    provider_id=self._descriptor.provider_id,
                    effect=ModelFailureEffect.NO_EFFECT,
                )

    def _command_authority_sha256(
        self,
        records: Mapping[int, ArtifactRecord],
    ) -> str:
        payload = {
            "argv": list(self._command),
            "environment": {
                key: self._environment[key]
                for key in sorted(self._environment)
            },
            "artifacts": [
                {
                    "argument_index": index,
                    "artifact_id": record.artifact_id,
                    "sha256": record.sha256,
                    "size_bytes": record.size_bytes,
                    "kind": record.kind,
                }
                for index, record in sorted(records.items())
            ],
        }
        encoded = _canonical_json_bytes(
            payload,
            max_bytes=self._max_request_bytes,
            label="evaluation command authority",
        )
        return hashlib.sha256(b"nika-evaluation-attestor-v1\x00" + encoded).hexdigest()

    def _request_payload(
        self,
        request: ModelRequest,
        binding: TrainingEvaluationBinding,
    ) -> dict[str, object]:
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "request": {
                "request_id": request.request_id,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                "model": request.model,
                "provider_id": request.provider_id,
                "provider_kind": request.provider_kind.value
                if request.provider_kind is not None
                else None,
                "privacy": request.privacy.value,
                "timeout_seconds": float(request.timeout_seconds),
                "temperature": request.temperature,
                "metadata": dict(request.metadata),
            },
            "binding": {
                "binding_sha256": binding.binding_sha256,
                "challenger_candidate_id": binding.challenger_candidate_id,
                "artifact_sha256": binding.challenger_sha256,
                "artifact_size_bytes": binding.challenger_size_bytes,
                "descriptor_digest": binding.descriptor_digest,
                "descriptor_registry_key": binding.descriptor_registry_key,
            },
            "candidate": {
                "path": self._candidate_path,
                "sha256": binding.challenger_sha256,
                "size_bytes": binding.challenger_size_bytes,
            },
            "attestor": {
                "attestor_id": self._attestor_id,
                "attestor_sha256": self._attestor_sha256,
            },
        }

    async def _execute(
        self,
        request_bytes: bytes,
        *,
        provider_id: str,
        timeout_seconds: float,
    ) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                env=dict(self._environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise _error(
                ModelErrorCode.UNAVAILABLE,
                "evaluation subprocess executable was not found",
                provider_id=provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            ) from exc
        except PermissionError as exc:
            raise _error(
                ModelErrorCode.UNAVAILABLE,
                "evaluation subprocess executable is not executable",
                provider_id=provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            ) from exc
        except OSError as exc:
            raise _error(
                ModelErrorCode.UNAVAILABLE,
                "evaluation subprocess could not be started",
                provider_id=provider_id,
                effect=ModelFailureEffect.NO_EFFECT,
            ) from exc

        if process.stdin is None or process.stdout is None:
            await self._terminate(process)
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess streams are unavailable",
                provider_id=provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            )

        try:
            try:
                await asyncio.to_thread(
                    self._verify_command_records,
                    self._command_records,
                )
            except ModelGatewayError as exc:
                await self._terminate(process)
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation command authority changed across process start",
                    provider_id=provider_id,
                    effect=ModelFailureEffect.UNKNOWN,
                ) from exc

            async def write_request() -> None:
                assert process.stdin is not None
                process.stdin.write(request_bytes)
                await process.stdin.drain()
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    return

            async def read_response() -> bytes:
                assert process.stdout is not None
                captured = bytearray()
                while True:
                    remaining = self._max_response_bytes + 1 - len(captured)
                    if remaining <= 0:
                        raise ValueError("evaluation subprocess response exceeds byte limit")
                    chunk = await process.stdout.read(min(_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    captured.extend(chunk)
                    if len(captured) > self._max_response_bytes:
                        raise ValueError("evaluation subprocess response exceeds byte limit")
                return bytes(captured)

            writer = asyncio.create_task(write_request())
            reader = asyncio.create_task(read_response())
            waiter = asyncio.create_task(process.wait())
            try:
                async with asyncio.timeout(timeout_seconds):
                    _, raw_response, returncode = await asyncio.gather(
                        writer,
                        reader,
                        waiter,
                    )
            except TimeoutError as exc:
                await self._terminate(process)
                raise _error(
                    ModelErrorCode.TIMEOUT,
                    "evaluation subprocess timed out",
                    provider_id=provider_id,
                    effect=ModelFailureEffect.UNKNOWN,
                ) from exc
            except asyncio.CancelledError:
                await self._terminate(process)
                raise
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
                await self._terminate(process)
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation subprocess transport failed",
                    provider_id=provider_id,
                    effect=ModelFailureEffect.UNKNOWN,
                ) from exc
            finally:
                for task in (writer, reader, waiter):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(writer, reader, waiter, return_exceptions=True)

            if returncode != 0:
                raise _error(
                    ModelErrorCode.PROVIDER_ERROR,
                    "evaluation subprocess exited unsuccessfully",
                    provider_id=provider_id,
                    effect=ModelFailureEffect.UNKNOWN,
                )
            return raw_response
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        except (ChildProcessError, ProcessLookupError):
            return

    def _parse_response(
        self,
        raw_response: bytes,
        *,
        request: ModelRequest,
        binding: TrainingEvaluationBinding,
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
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess returned invalid JSON",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            ) from exc

        expected_keys = {
            "protocol_version",
            "request_id",
            "provider_id",
            "model",
            "text",
            "loaded_artifact_sha256",
            "loaded_artifact_size_bytes",
            "descriptor_digest",
            "usage",
        }
        if type(response) is not dict or set(response) != expected_keys:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess returned an invalid response schema",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            )
        try:
            _validate_json_tree(response, label="evaluation subprocess response")
        except ValueError as exc:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess response exceeds protocol bounds",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            ) from exc

        if (
            type(response["protocol_version"]) is not int
            or response["protocol_version"] != _PROTOCOL_VERSION
            or type(response["request_id"]) is not str
            or response["request_id"] != request.request_id
            or type(response["provider_id"]) is not str
            or response["provider_id"] != binding.challenger_provider_id
            or type(response["model"]) is not str
            or response["model"] != binding.challenger_model_id
            or type(response["loaded_artifact_sha256"]) is not str
            or response["loaded_artifact_sha256"] != binding.challenger_sha256
            or type(response["loaded_artifact_size_bytes"]) is not int
            or response["loaded_artifact_size_bytes"] != binding.challenger_size_bytes
            or type(response["descriptor_digest"]) is not str
            or response["descriptor_digest"] != binding.descriptor_digest
        ):
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess identity evidence does not match the effect",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            )

        response_text = response["text"]
        if type(response_text) is not str or not response_text:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess returned invalid response text",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            )
        try:
            response_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess returned invalid response text",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            ) from exc

        usage = response["usage"]
        if type(usage) is not dict or set(usage) != {
            "input_tokens",
            "output_tokens",
            "total_tokens",
        }:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess returned invalid usage evidence",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            )
        try:
            input_tokens = _usage_value(usage["input_tokens"], name="input_tokens")
            output_tokens = _usage_value(usage["output_tokens"], name="output_tokens")
            total_tokens = _usage_value(usage["total_tokens"], name="total_tokens")
        except ValueError as exc:
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess returned invalid usage evidence",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            ) from exc
        if (
            input_tokens is not None
            and output_tokens is not None
            and total_tokens is not None
            and total_tokens != input_tokens + output_tokens
        ):
            raise _error(
                ModelErrorCode.PROVIDER_ERROR,
                "evaluation subprocess token totals are inconsistent",
                provider_id=binding.challenger_provider_id,
                effect=ModelFailureEffect.UNKNOWN,
            )

        response["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        return response
