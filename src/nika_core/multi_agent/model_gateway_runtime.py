from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)


class ModelGatewayAgentRuntime:
    """Thin AgentRuntimePort adapter for one exact configured ModelGateway route.

    The adapter deliberately does not implement model acquisition, tool execution,
    durable inference checkpoints, or provider fallback. It exists only to let the
    multi-agent supervisor use the same ModelGateway contract for local or API
    providers.
    """

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        definitions: AgentDefinitionRepository,
        provider_id: str = "ollama",
        provider_kind: ProviderKind = ProviderKind.LOCAL,
        model: str | None = None,
        timeout_seconds: float = 60.0,
        privacy: PrivacyClass = PrivacyClass.PRIVATE,
        temperature: float | None = 0.0,
    ) -> None:
        if not provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if provider_id != provider_id.strip():
            raise ValueError("provider_id must not contain surrounding whitespace")
        if not isinstance(provider_kind, ProviderKind):
            raise TypeError("provider_kind must be a ProviderKind")
        if model is not None:
            if not model.strip():
                raise ValueError("model must not be blank when provided")
            if model != model.strip():
                raise ValueError("model must not contain surrounding whitespace")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._gateway = gateway
        self._definitions = definitions
        self._provider_id = provider_id
        self._provider_kind = provider_kind
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._privacy = privacy
        self._temperature = temperature
        self._active: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._active_lock = asyncio.Lock()

    @property
    def runtime_id(self) -> str:
        return f"model-gateway:{self._provider_kind.value}:{self._provider_id}"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        capabilities = {
            RuntimeCapability.CANCELLATION,
            RuntimeCapability.PARALLELISM,
            RuntimeCapability.SUBAGENTS,
        }
        if self._provider_kind is ProviderKind.LOCAL:
            capabilities.add(RuntimeCapability.LOCAL_MODELS)
        return frozenset(capabilities)

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        try:
            model_request = self._build_model_request(request)
        except (KeyError, TypeError, ValueError, PermissionError) as exc:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                output={"recoverable": False, "reason": type(exc).__name__},
                error="The agent request is invalid and cannot be sent to the model provider.",
                error_code=RuntimeErrorCode.INTERNAL,
            )

        key = (request.task_id, request.thread_id)
        async with self._active_lock:
            existing = self._active.get(key)
            if existing is not None and not existing.done():
                return RuntimeResult(
                    outcome=RuntimeOutcome.FAILED,
                    output={"recoverable": True, "provider_id": self._provider_id},
                    error="This agent already has an active model request.",
                    error_code=RuntimeErrorCode.DUPLICATE_ACTIVE,
                )
            task = asyncio.create_task(self._gateway.complete(model_request))
            self._active[key] = task

        try:
            response = await task
        except asyncio.CancelledError:
            return RuntimeResult(
                outcome=RuntimeOutcome.CANCELLED,
                output={
                    "message": "Model request cancelled.",
                    "provider_id": self._provider_id,
                },
            )
        except ModelGatewayError as exc:
            return self._failure_result(exc)
        finally:
            async with self._active_lock:
                if self._active.get(key) is task:
                    self._active.pop(key, None)

        if response.request_id != model_request.request_id:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                output={"recoverable": False, "provider_id": self._provider_id},
                error="The model provider returned a response for a different request.",
                error_code=RuntimeErrorCode.INTERNAL,
            )
        if (
            response.provider_id != self._provider_id
            or response.provider_kind is not self._provider_kind
        ):
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                output={
                    "recoverable": False,
                    "provider_id": response.provider_id,
                    "provider_kind": response.provider_kind.value,
                },
                error="The model response did not match the configured provider route.",
                error_code=RuntimeErrorCode.INTERNAL,
            )

        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={
                "text": response.text,
                "provider_id": response.provider_id,
                "provider_kind": response.provider_kind.value,
                "model_fingerprint": model_identity_fingerprint(response.model),
                "latency_ms": response.latency_ms,
            },
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(
            outcome=RuntimeOutcome.FAILED,
            output={"recoverable": True, "provider_id": self._provider_id},
            error=(
                "This model request cannot resume from an opaque inference checkpoint. "
                "Restart it from durable task state instead."
            ),
            error_code=RuntimeErrorCode.INVALID_RESUME,
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        key = (task_id, thread_id)
        async with self._active_lock:
            task = self._active.get(key)
            if task is None or task.done():
                return False
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except ModelGatewayError:
            pass
        return True

    def _build_model_request(self, request: RuntimeRequest) -> ModelRequest:
        agent_id = self._required_text(request.payload, "agent_id")
        agent_version = self._required_int(request.payload, "agent_version")
        stored = self._definitions.require_active(agent_id, agent_version)
        definition = stored.definition
        handoff = request.payload.get("handoff", {})
        if not isinstance(handoff, Mapping):
            raise TypeError("handoff must be a mapping")
        system_text = (
            f"You are {definition.name}.\n"
            f"Goal: {definition.goal}\n"
            "Instructions:\n"
            f"{definition.instructions}"
        )
        user_text = (
            "Complete the assigned handoff. Return the result text only.\n"
            + json.dumps(dict(handoff), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        timeout = request.timeout_seconds or self._timeout_seconds
        return ModelRequest(
            request_id=f"{request.task_id}:{request.thread_id}",
            messages=(
                ModelMessage(role="system", content=system_text),
                ModelMessage(role="user", content=user_text),
            ),
            provider_id=self._provider_id,
            provider_kind=None,
            model=self._model,
            privacy=self._privacy,
            timeout_seconds=timeout,
            temperature=self._temperature,
        )

    def _failure_result(self, error: ModelGatewayError) -> RuntimeResult:
        message = {
            ModelErrorCode.UNAVAILABLE: (
                "The configured model provider is unavailable. Start or configure it, then retry."
            ),
            ModelErrorCode.TIMEOUT: "The model request timed out. Retry when the provider is ready.",
            ModelErrorCode.CANCELLED: "The model request was cancelled.",
            ModelErrorCode.RESOURCE_LIMIT: (
                "The model provider cannot run within the current resource limits. "
                "Free resources or choose another configured model, then retry."
            ),
            ModelErrorCode.AUTHENTICATION: (
                "The configured model provider rejected authentication. Check its configuration and retry."
            ),
            ModelErrorCode.RATE_LIMITED: (
                "The configured model provider is temporarily rate limited. Retry later."
            ),
            ModelErrorCode.INVALID_REQUEST: (
                "The configured model route rejected this request. Check the model configuration."
            ),
            ModelErrorCode.POLICY_DENIED: (
                "The configured model route is not permitted for this task."
            ),
            ModelErrorCode.PROVIDER_ERROR: (
                "The model provider could not complete the request. "
                "Check that the provider and model are available, then retry."
            ),
        }[error.code]
        if error.code is ModelErrorCode.CANCELLED:
            return RuntimeResult(
                outcome=RuntimeOutcome.CANCELLED,
                output={"message": message, "provider_id": error.provider_id or self._provider_id},
            )
        runtime_code = (
            RuntimeErrorCode.TIMEOUT
            if error.code is ModelErrorCode.TIMEOUT
            else RuntimeErrorCode.TRANSIENT
            if error.code
            in {
                ModelErrorCode.UNAVAILABLE,
                ModelErrorCode.RATE_LIMITED,
                ModelErrorCode.PROVIDER_ERROR,
                ModelErrorCode.RESOURCE_LIMIT,
            }
            else RuntimeErrorCode.INTERNAL
        )
        recoverable = error.code not in {
            ModelErrorCode.AUTHENTICATION,
            ModelErrorCode.INVALID_REQUEST,
            ModelErrorCode.POLICY_DENIED,
        }
        return RuntimeResult(
            outcome=RuntimeOutcome.FAILED,
            output={
                "model_error_code": error.code.value,
                "provider_id": error.provider_id or self._provider_id,
                "recoverable": recoverable,
                "provider_retryable": error.retryable,
            },
            error=message,
            error_code=runtime_code,
        )

    @staticmethod
    def _required_text(payload: Mapping[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be non-empty text")
        return value

    @staticmethod
    def _required_int(payload: Mapping[str, object], key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
        return value
