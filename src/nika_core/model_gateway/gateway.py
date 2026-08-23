from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Protocol

from .contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderCostClass,
    ProviderKind,
    ProviderResourceClass,
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


class _UntypedProviderTimeout(Exception):
    """Internal marker separating provider bugs from the gateway deadline."""


async def _invoke_provider(provider: ModelProvider, request: ModelRequest) -> ModelResponse:
    try:
        return await provider.complete(request)
    except TimeoutError as exc:
        raise _UntypedProviderTimeout from exc


_NO_FALLBACK_CODES = frozenset(
    {
        ModelErrorCode.INVALID_REQUEST,
        ModelErrorCode.CANCELLED,
        ModelErrorCode.AUTHENTICATION,
        ModelErrorCode.POLICY_DENIED,
        ModelErrorCode.RESOURCE_LIMIT,
    }
)
_SAFE_FALLBACK_CODES = frozenset(
    {
        ModelErrorCode.UNAVAILABLE,
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.TIMEOUT,
    }
)


class ModelGateway:
    def __init__(self, *, audit_log: _AuditLogPort | None = None) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._defaults: dict[ProviderKind, str] = {}
        self._audit_log = audit_log

    def register(self, provider: ModelProvider, *, default: bool = False) -> None:
        capabilities = provider.capabilities
        provider_id = capabilities.provider_id
        if provider_id in self._providers:
            raise ValueError(f"duplicate provider_id: {provider_id}")
        if default and capabilities.kind in self._defaults:
            existing = self._defaults[capabilities.kind]
            raise ValueError(
                f"default provider already registered for {capabilities.kind.value}: {existing}"
            )
        self._providers[provider_id] = provider
        if default:
            self._defaults[capabilities.kind] = provider_id

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def provider_capabilities(self) -> tuple[ProviderCapabilities, ...]:
        return tuple(
            self._providers[provider_id].capabilities
            for provider_id in sorted(self._providers)
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            providers = self._select_candidates(request)
            self._validate_route(request, providers)
        except ModelGatewayError as error:
            self._audit_preflight_failure(request, error)
            raise

        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds

        for index, provider in enumerate(providers):
            capabilities = provider.capabilities
            remaining = deadline - loop.time()
            if remaining <= 0:
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "model request exceeded its total deadline",
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
            cost_class = self._effective_cost_class(capabilities)
            resource_class = self._effective_resource_class(capabilities)
            self._audit(
                event_type="model.requested",
                request=request,
                payload={
                    "provider_id": capabilities.provider_id,
                    "provider_kind": capabilities.kind.value,
                    "privacy": request.privacy.value,
                    "model": request.model or "default",
                    "attempt": index + 1,
                    "cost_class": cost_class.value,
                    "resource_class": resource_class.value,
                },
            )

            try:
                response = await asyncio.wait_for(
                    _invoke_provider(provider, attempt_request), timeout=remaining
                )
            except _UntypedProviderTimeout as exc:
                error = ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "model provider raised an untyped timeout failure",
                    provider_id=capabilities.provider_id,
                    retryable=False,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                raise error from exc
            except TimeoutError as exc:
                retryable = capabilities.supports_hard_cancellation
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "model request exceeded its total deadline",
                    provider_id=capabilities.provider_id,
                    retryable=retryable,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                if self._can_fallback(error=error, index=index, providers=providers):
                    self._audit_fallback(request, provider, providers[index + 1], error)
                    continue
                raise error from exc
            except asyncio.CancelledError:
                self._audit(
                    event_type="model.cancelled",
                    request=request,
                    payload={"provider_id": capabilities.provider_id},
                )
                raise
            except ModelGatewayError as raw_error:
                error = self._normalize_provider_error(raw_error, capabilities.provider_id)
                self._audit_failure(request, capabilities.provider_id, error)
                if self._can_fallback(error=error, index=index, providers=providers):
                    self._audit_fallback(request, provider, providers[index + 1], error)
                    continue
                if error is raw_error:
                    raise
                raise error from raw_error
            except Exception as exc:
                error = ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "model provider failed without a typed Nika error",
                    provider_id=capabilities.provider_id,
                    retryable=False,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                raise error from exc

            try:
                self._validate_response(request, capabilities, response)
            except (TypeError, ValueError) as exc:
                error = ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "model provider returned an invalid normalized response",
                    provider_id=capabilities.provider_id,
                    retryable=False,
                )
                self._audit_failure(request, capabilities.provider_id, error)
                raise error from exc

            self._audit(
                event_type="model.completed",
                request=request,
                payload={
                    "provider_id": response.provider_id,
                    "model": response.model,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "latency_ms": response.latency_ms,
                    "attempt": index + 1,
                    "cost_class": cost_class.value,
                    "resource_class": resource_class.value,
                },
            )
            return response

        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "model fallback route was exhausted",
            retryable=False,
        )

    def _select_candidates(self, request: ModelRequest) -> tuple[ModelProvider, ...]:
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

    def _validate_route(
        self, request: ModelRequest, providers: tuple[ModelProvider, ...]
    ) -> None:
        for provider in providers:
            capabilities = provider.capabilities
            provider_id = capabilities.provider_id
            if (
                request.privacy in {PrivacyClass.PRIVATE, PrivacyClass.SENSITIVE}
                and not capabilities.supports_private_data
            ):
                raise ModelGatewayError(
                    ModelErrorCode.POLICY_DENIED,
                    "private data cannot be routed to this provider",
                    provider_id=provider_id,
                    retryable=False,
                )
            if request.route_policy.local_only and capabilities.kind is ProviderKind.CLOUD:
                raise ModelGatewayError(
                    ModelErrorCode.POLICY_DENIED,
                    "model route is restricted to local providers",
                    provider_id=provider_id,
                    retryable=False,
                )

            cost_class = self._effective_cost_class(capabilities)
            if not request.route_policy.allow_metered and cost_class in {
                ProviderCostClass.METERED,
                ProviderCostClass.UNKNOWN,
            }:
                raise ModelGatewayError(
                    ModelErrorCode.POLICY_DENIED,
                    "model route does not allow metered or unknown-cost providers",
                    provider_id=provider_id,
                    retryable=False,
                )

            allowed_resources = request.route_policy.allowed_resource_classes
            resource_class = self._effective_resource_class(capabilities)
            if allowed_resources and resource_class not in allowed_resources:
                raise ModelGatewayError(
                    ModelErrorCode.RESOURCE_LIMIT,
                    "model provider resource class is not allowed for this request",
                    provider_id=provider_id,
                    retryable=False,
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
        if error.provider_id is not None and error.provider_id != provider_id:
            return ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an error for another provider identity",
                provider_id=provider_id,
                retryable=False,
            )
        if error.provider_id is None:
            return ModelGatewayError(
                error.code,
                str(error),
                provider_id=provider_id,
                retryable=error.retryable,
            )
        return error

    @staticmethod
    def _validate_response(
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        response: ModelResponse,
    ) -> None:
        if not isinstance(response, ModelResponse):
            raise TypeError("provider response must be ModelResponse")
        if not isinstance(response.text, str):
            raise TypeError("provider response text must be a string")
        if not response.text.strip():
            raise ValueError("provider response text must not be empty")
        if response.request_id != request.request_id:
            raise ValueError("provider response request identity does not match")
        if response.provider_id != capabilities.provider_id:
            raise ValueError("provider response provider identity does not match")
        if response.provider_kind is not capabilities.kind:
            raise ValueError("provider response provider kind does not match")
        if request.model is not None and response.model != request.model:
            raise ValueError("provider response model identity does not match requested model")

    @staticmethod
    def _can_fallback(
        *, error: ModelGatewayError, index: int, providers: tuple[ModelProvider, ...]
    ) -> bool:
        if index + 1 >= len(providers):
            return False
        if error.code in _NO_FALLBACK_CODES:
            return False
        if error.code not in _SAFE_FALLBACK_CODES:
            return False
        if not error.retryable:
            return False
        return not (
            error.code is ModelErrorCode.TIMEOUT
            and not providers[index].capabilities.supports_hard_cancellation
        )

    @staticmethod
    def _effective_cost_class(capabilities: ProviderCapabilities) -> ProviderCostClass:
        if capabilities.cost_class is not None:
            return capabilities.cost_class
        if capabilities.kind is ProviderKind.NO_LLM:
            return ProviderCostClass.NONE
        if capabilities.kind is ProviderKind.LOCAL:
            return ProviderCostClass.LOCAL_RESOURCE
        return ProviderCostClass.UNKNOWN

    @staticmethod
    def _effective_resource_class(
        capabilities: ProviderCapabilities,
    ) -> ProviderResourceClass:
        if capabilities.resource_class is not None:
            return capabilities.resource_class
        if capabilities.kind is ProviderKind.NO_LLM:
            return ProviderResourceClass.NONE
        return ProviderResourceClass.UNKNOWN

    def _audit_preflight_failure(
        self, request: ModelRequest, error: ModelGatewayError
    ) -> None:
        payload: dict[str, object] = {"code": error.code.value, "phase": "preflight"}
        if error.provider_id is not None and error.provider_id in self._providers:
            payload["provider_id"] = error.provider_id
        self._audit(event_type="model.failed", request=request, payload=payload)

    def _audit_failure(
        self, request: ModelRequest, provider_id: str, error: ModelGatewayError
    ) -> None:
        self._audit(
            event_type="model.failed",
            request=request,
            payload={"provider_id": provider_id, "code": error.code.value},
        )

    def _audit_fallback(
        self,
        request: ModelRequest,
        current: ModelProvider,
        fallback: ModelProvider,
        error: ModelGatewayError,
    ) -> None:
        self._audit(
            event_type="model.fallback",
            request=request,
            payload={
                "from_provider_id": current.capabilities.provider_id,
                "to_provider_id": fallback.capabilities.provider_id,
                "reason": error.code.value,
            },
        )

    def _select(self, request: ModelRequest) -> ModelProvider:
        if request.provider_id:
            provider = self._providers.get(request.provider_id)
            if provider is None:
                raise ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    f"unknown model provider: {request.provider_id}",
                    provider_id=request.provider_id,
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
