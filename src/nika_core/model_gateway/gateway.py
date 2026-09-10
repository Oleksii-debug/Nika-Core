from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from math import isfinite
from typing import Protocol

from .contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)


class _AuditLogPort(Protocol):
    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    provider: ModelProvider
    capabilities: ProviderCapabilities


_SAFE_FALLBACK_CODES = frozenset(
    {
        ModelErrorCode.UNAVAILABLE,
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.TIMEOUT,
    }
)
_MAX_DURABLE_TOKEN_COUNT = (1 << 63) - 1

_SAFE_PROVIDER_MESSAGES = {
    ModelErrorCode.INVALID_REQUEST: "model provider rejected the request",
    ModelErrorCode.UNAVAILABLE: "model provider is unavailable",
    ModelErrorCode.TIMEOUT: "model provider request timed out",
    ModelErrorCode.CANCELLED: "model provider request was cancelled",
    ModelErrorCode.AUTHENTICATION: "model provider authentication failed",
    ModelErrorCode.RATE_LIMITED: "model provider rate limit was reached",
    ModelErrorCode.RESOURCE_LIMIT: "model provider resource limit was reached",
    ModelErrorCode.PROVIDER_ERROR: "model provider failed",
}


class ModelGateway:
    def __init__(self, *, audit_log: _AuditLogPort | None = None) -> None:
        self._providers: dict[str, _RegisteredProvider] = {}
        self._defaults: dict[ProviderKind, str] = {}
        self._audit_log = audit_log

    def register(self, provider: ModelProvider, *, default: bool = False) -> None:
        capabilities = self._read_capabilities_for_registration(provider)
        provider_id = capabilities.provider_id
        if provider_id in self._providers:
            raise ValueError(f"duplicate provider_id: {provider_id}")
        self._providers[provider_id] = _RegisteredProvider(
            provider=provider,
            capabilities=capabilities,
        )
        if default:
            self._defaults[capabilities.kind] = provider_id

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        providers = self._select_candidates(request)
        self._validate_privacy_route(request, providers)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds

        for index, registered in enumerate(providers):
            self._assert_capability_contract(registered)
            provider = registered.provider
            capabilities = registered.capabilities
            remaining = deadline - loop.time()
            if remaining <= 0:
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "model request exceeded its deadline",
                    provider_id=capabilities.provider_id,
                    retryable=False,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                raise error

            attempt_request = replace(
                request,
                provider_id=capabilities.provider_id,
                provider_kind=None,
                fallback_provider_ids=(),
                timeout_seconds=remaining,
            )
            self._audit(
                event_type="model.requested",
                request=request,
                payload={
                    "provider_id": capabilities.provider_id,
                    "provider_kind": capabilities.kind.value,
                    "privacy": request.privacy.value,
                    "model_fingerprint": model_identity_fingerprint(request.model),
                    "attempt": index + 1,
                },
            )

            response: ModelResponse | None = None
            terminal_error: ModelGatewayError | None = None
            cancelled = False
            try:
                response = await asyncio.wait_for(
                    provider.complete(attempt_request), timeout=remaining
                )
            except TimeoutError:
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "model request exceeded its deadline",
                    provider_id=capabilities.provider_id,
                    retryable=capabilities.supports_hard_cancellation,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                if self._can_fallback(error=error, index=index, providers=providers):
                    self._audit_fallback(request, registered, providers[index + 1], error)
                    continue
                terminal_error = error
            except asyncio.CancelledError:
                self._audit(
                    event_type="model.cancelled",
                    request=request,
                    payload={"provider_id": capabilities.provider_id},
                )
                cancelled = True
            except ModelGatewayError as raw_error:
                error = self._normalize_provider_error(
                    raw_error, capabilities.provider_id
                )
                self._audit_failure(request, capabilities.provider_id, error)
                if self._can_fallback(error=error, index=index, providers=providers):
                    self._audit_fallback(request, registered, providers[index + 1], error)
                    continue
                terminal_error = error
            except Exception:  # noqa: BLE001 - provider implementations are untrusted
                error = ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "model provider failed without a typed Nika error",
                    provider_id=capabilities.provider_id,
                    retryable=False,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                terminal_error = error

            # Raise after the provider exception handler so provider-controlled
            # diagnostics are not retained as public cause/context chains.
            if cancelled:
                raise asyncio.CancelledError()
            if terminal_error is not None:
                raise terminal_error
            if response is None:
                error = ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "model provider completed without a response",
                    provider_id=capabilities.provider_id,
                    retryable=False,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                raise error

            response_error = self._validate_success_response(
                response=response,
                request=request,
                trusted_provider_id=capabilities.provider_id,
                trusted_provider_kind=capabilities.kind,
            )
            if response_error is not None:
                self._audit_failure(
                    request,
                    capabilities.provider_id,
                    response_error,
                )
                raise response_error

            self._audit(
                event_type="model.completed",
                request=request,
                payload={
                    "provider_id": response.provider_id,
                    "model_fingerprint": model_identity_fingerprint(response.model),
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "latency_ms": response.latency_ms,
                    "attempt": index + 1,
                },
            )
            return response

        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "model fallback route was exhausted",
            retryable=True,
        )

    def _select_candidates(self, request: ModelRequest) -> tuple[_RegisteredProvider, ...]:
        primary = self._select(request)
        candidates = [primary]
        seen = {primary.capabilities.provider_id}
        for provider_id in request.fallback_provider_ids:
            if provider_id in seen:
                raise ModelGatewayError(
                    ModelErrorCode.INVALID_REQUEST,
                    f"fallback route repeats provider: {provider_id}",
                    provider_id=provider_id,
                )
            provider = self._providers.get(provider_id)
            if provider is None:
                raise ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    f"unknown fallback model provider: {provider_id}",
                    provider_id=provider_id,
                )
            candidates.append(provider)
            seen.add(provider_id)
        return tuple(candidates)

    def _validate_privacy_route(
        self, request: ModelRequest, providers: tuple[_RegisteredProvider, ...]
    ) -> None:
        for provider in providers:
            self._assert_capability_contract(provider)
            capabilities = provider.capabilities
            if (
                request.privacy is not PrivacyClass.PUBLIC
                and not capabilities.supports_private_data
            ):
                raise ModelGatewayError(
                    ModelErrorCode.INVALID_REQUEST,
                    "private data cannot be routed to this provider",
                    provider_id=capabilities.provider_id,
                )

    @staticmethod
    def _read_capabilities_for_registration(
        provider: ModelProvider,
    ) -> ProviderCapabilities:
        try:
            capabilities = provider.capabilities
        except Exception:  # noqa: BLE001 - provider metadata is untrusted
            raise ValueError("provider capabilities could not be read") from None
        return ModelGateway._validate_capability_value(capabilities)

    @staticmethod
    def _validate_capability_value(capabilities: object) -> ProviderCapabilities:
        if type(capabilities) is not ProviderCapabilities:
            raise ValueError("provider capabilities must use ProviderCapabilities")
        if (
            not isinstance(capabilities.provider_id, str)
            or not capabilities.provider_id.strip()
            or capabilities.provider_id != capabilities.provider_id.strip()
        ):
            raise ValueError("provider capabilities require a normalized provider_id")
        if not isinstance(capabilities.kind, ProviderKind):
            raise ValueError("provider capabilities require a ProviderKind")
        for field_name in (
            "supports_private_data",
            "supports_tools",
            "supports_streaming",
            "supports_hard_cancellation",
        ):
            if type(getattr(capabilities, field_name)) is not bool:
                raise ValueError(f"provider capability {field_name} must be boolean")
        return capabilities

    @staticmethod
    def _assert_capability_contract(provider: _RegisteredProvider) -> None:
        try:
            current = ModelGateway._validate_capability_value(
                provider.provider.capabilities
            )
        except Exception:  # noqa: BLE001 - provider metadata is untrusted
            current = None
        if current == provider.capabilities:
            return
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "model provider capability contract changed after registration",
            provider_id=provider.capabilities.provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )

    @staticmethod
    def _validate_success_response(
        *,
        response: object,
        request: ModelRequest,
        trusted_provider_id: str,
        trusted_provider_kind: ProviderKind,
    ) -> ModelGatewayError | None:
        if not isinstance(response, ModelResponse):
            return ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an invalid success response",
                provider_id=trusted_provider_id,
                retryable=False,
                failure_effect=ModelFailureEffect.UNKNOWN,
            )

        invalid = (
            response.request_id != request.request_id
            or response.provider_id != trusted_provider_id
            or response.provider_kind is not trusted_provider_kind
            or not isinstance(response.text, str)
            or not isinstance(response.model, str)
            or not response.model
            or (request.model is not None and response.model != request.model)
            or not isinstance(response.usage, ModelUsage)
        )
        if not invalid:
            for value in (
                response.usage.input_tokens,
                response.usage.output_tokens,
                response.usage.total_tokens,
            ):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > _MAX_DURABLE_TOKEN_COUNT
                ):
                    invalid = True
                    break

        if not invalid and response.latency_ms is not None:
            latency = response.latency_ms
            if isinstance(latency, bool) or not isinstance(latency, (int, float)):
                invalid = True
            else:
                try:
                    finite_latency = isfinite(float(latency))
                except OverflowError:
                    finite_latency = False
                if not finite_latency or latency < 0:
                    invalid = True

        if not invalid:
            return None
        return ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "model provider returned an invalid success response",
            provider_id=trusted_provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )

    @staticmethod
    def _normalize_provider_error(
        error: ModelGatewayError, provider_id: str
    ) -> ModelGatewayError:
        if not isinstance(error.code, ModelErrorCode):
            return ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an invalid error code",
                provider_id=provider_id,
                retryable=False,
            )
        if not isinstance(error.retryable, bool):
            return ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an invalid retryable flag",
                provider_id=provider_id,
                retryable=False,
            )
        if not isinstance(error.failure_effect, ModelFailureEffect):
            return ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an invalid failure effect state",
                provider_id=provider_id,
                retryable=False,
            )
        if error.provider_id is not None and error.provider_id != provider_id:
            return ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an error for another provider identity",
                provider_id=provider_id,
                retryable=False,
            )
        safe_message = _SAFE_PROVIDER_MESSAGES[error.code]
        if provider_id == "foundry-local" and error.code is ModelErrorCode.UNAVAILABLE:
            safe_message = (
                "Foundry Local model is unavailable; use the explicit model download "
                "action before inference if the model is not cached"
            )
        return ModelGatewayError(
            error.code,
            safe_message,
            provider_id=provider_id,
            retryable=error.retryable,
            failure_effect=error.failure_effect,
        )

    @staticmethod
    def _can_fallback(
        *,
        error: ModelGatewayError,
        index: int,
        providers: tuple[_RegisteredProvider, ...],
    ) -> bool:
        if index + 1 >= len(providers):
            return False
        if error.code not in _SAFE_FALLBACK_CODES:
            return False
        if not error.retryable:
            return False
        if error.failure_effect is not ModelFailureEffect.NO_EFFECT:
            return False
        return not (
            error.code is ModelErrorCode.TIMEOUT
            and not providers[index].capabilities.supports_hard_cancellation
        )

    def _audit_failure(
        self, request: ModelRequest, provider_id: str, error: ModelGatewayError
    ) -> None:
        self._audit(
            event_type="model.failed",
            request=request,
            payload={
                "provider_id": provider_id,
                "model_fingerprint": model_identity_fingerprint(request.model),
                "code": error.code.value,
                "failure_effect": error.failure_effect.value,
            },
        )

    def _audit_fallback(
        self,
        request: ModelRequest,
        current: _RegisteredProvider,
        fallback: _RegisteredProvider,
        error: ModelGatewayError,
    ) -> None:
        self._audit(
            event_type="model.fallback",
            request=request,
            payload={
                "from_provider_id": current.capabilities.provider_id,
                "to_provider_id": fallback.capabilities.provider_id,
                "reason": error.code.value,
                "failure_effect": error.failure_effect.value,
            },
        )

    def _select(self, request: ModelRequest) -> _RegisteredProvider:
        if request.provider_id:
            provider = self._providers.get(request.provider_id)
            if provider is None:
                raise ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    f"unknown model provider: {request.provider_id}",
                    provider_id=request.provider_id,
                )
            if (
                request.provider_kind is not None
                and provider.capabilities.kind is not request.provider_kind
            ):
                raise ModelGatewayError(
                    ModelErrorCode.INVALID_REQUEST,
                    "selected model provider kind does not match the requested boundary",
                    provider_id=request.provider_id,
                    retryable=False,
                    failure_effect=ModelFailureEffect.NO_EFFECT,
                )
            return provider
        if request.provider_kind:
            provider_id = self._defaults.get(request.provider_kind)
            if provider_id is None:
                raise ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    f"no default provider for kind: {request.provider_kind.value}",
                )
            return self._providers[provider_id]
        if len(self._providers) == 1:
            return next(iter(self._providers.values()))
        raise ModelGatewayError(
            ModelErrorCode.INVALID_REQUEST,
            "provider_id or provider_kind is required when several providers are registered",
        )

    def _audit(
        self,
        *,
        event_type: str,
        request: ModelRequest,
        payload: dict[str, object],
    ) -> None:
        if self._audit_log is None:
            return
        self._audit_log.append(
            event_type=event_type,
            entity_type="model_request",
            entity_id=request.request_id,
            payload=payload,
        )


def model_identity_fingerprint(model: str | None) -> str:
    """Return a stable content-free projection for untrusted model identity metadata."""

    value = model if model is not None else "<provider-default>"
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"sha256:{digest}"
