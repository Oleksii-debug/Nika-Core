from __future__ import annotations

import asyncio

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
        provider = self._select(request)
        capabilities = provider.capabilities
        if request.privacy is PrivacyClass.SENSITIVE and not capabilities.supports_private_data:
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                "sensitive data cannot be routed to this provider",
                provider_id=capabilities.provider_id,
            )
        self._audit(
            event_type="model.requested",
            request=request,
            payload={
                "provider_id": capabilities.provider_id,
                "provider_kind": capabilities.kind.value,
                "privacy": request.privacy.value,
                "model": request.model or "default",
            },
        )
        try:
            response = await asyncio.wait_for(
                provider.complete(request), timeout=request.timeout_seconds
            )
        except TimeoutError as exc:
            self._audit(
                event_type="model.failed",
                request=request,
                payload={"provider_id": capabilities.provider_id, "code": "timeout"},
            )
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "model request exceeded its deadline",
                provider_id=capabilities.provider_id,
                retryable=True,
            ) from exc
        except asyncio.CancelledError:
            self._audit(
                event_type="model.cancelled",
                request=request,
                payload={"provider_id": capabilities.provider_id},
            )
            raise
        except ModelGatewayError as exc:
            self._audit(
                event_type="model.failed",
                request=request,
                payload={"provider_id": capabilities.provider_id, "code": exc.code.value},
            )
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
            },
        )
        return response

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
