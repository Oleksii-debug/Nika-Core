from __future__ import annotations

import asyncio
from dataclasses import replace

from nika_core.kernel.audit import AuditLog
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)


class ModelGateway:
    def __init__(self, *, audit_log: AuditLog | None = None) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._defaults: dict[ProviderKind, str] = {}
        self._audit_log = audit_log

    def register(self, provider: ModelProvider, *, default: bool = False) -> None:
        provider_id = provider.capabilities.provider_id
        if provider_id in self._providers:
            raise ValueError(f"duplicate provider_id: {provider_id}")
        self._providers[provider_id] = provider
        if default:
            self._defaults[provider.capabilities.kind] = provider_id

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        providers = self._select_candidates(request)
        self._validate_privacy_route(request, providers)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds

        for index, provider in enumerate(providers):
            capabilities = provider.capabilities
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
                    "model": request.model or "default",
                    "attempt": index + 1,
                },
            )

            try:
                response = await asyncio.wait_for(
                    provider.complete(attempt_request), timeout=remaining
                )
            except TimeoutError as exc:
                retryable = capabilities.supports_hard_cancellation
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "model request exceeded its deadline",
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
            except ModelGatewayError as error:
                self._audit_failure(request, capabilities.provider_id, error)
                if self._can_fallback(error=error, index=index, providers=providers):
                    self._audit_fallback(request, provider, providers[index + 1], error)
                    continue
                raise

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
                },
            )
            return response

        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "model fallback route was exhausted",
            retryable=True,
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

    def _validate_privacy_route(
        self, request: ModelRequest, providers: tuple[ModelProvider, ...]
    ) -> None:
        if request.privacy is not PrivacyClass.SENSITIVE:
            return
        for provider in providers:
            capabilities = provider.capabilities
            if not capabilities.supports_private_data:
                raise ModelGatewayError(
                    ModelErrorCode.INVALID_REQUEST,
                    "sensitive data cannot be routed to this provider",
                    provider_id=capabilities.provider_id,
                )

    @staticmethod
    def _can_fallback(
        *, error: ModelGatewayError, index: int, providers: tuple[ModelProvider, ...]
    ) -> bool:
        return error.retryable and index + 1 < len(providers)

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
