from __future__ import annotations

import asyncio
from dataclasses import replace

from .contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
)

MAX_PROVIDER_ROUTE_ID_CHARS = 128


class RoutedModelProvider:
    """Give one existing provider instance a stable Nika-owned executable route ID.

    ``ProviderCapabilities.provider_id`` identifies the wrapped provider/engine at
    its native adapter boundary. ``route_id`` identifies this configured
    executable route at the outer ModelGateway boundary. Keeping those identities
    separate lets Nika register two endpoints/replicas of the same provider and
    model without changing the provider adapter's canonical identity.

    This class deliberately owns no persistence, provider selection, health,
    retry, credentials, scheduling or resource policy. Those remain with their
    existing canonical owners.
    """

    def __init__(self, *, route_id: str, provider: ModelProvider) -> None:
        _validate_route_id(route_id)
        capabilities = provider.capabilities
        if type(capabilities) is not ProviderCapabilities:
            raise TypeError("routed provider capabilities must be ProviderCapabilities")
        _validate_upstream_provider_id(capabilities.provider_id)
        self._route_id = route_id
        self._provider = provider
        self._upstream_capabilities = capabilities
        self._capabilities = replace(capabilities, provider_id=route_id)

    @property
    def route_id(self) -> str:
        return self._route_id

    @property
    def upstream_provider_id(self) -> str:
        return self._upstream_capabilities.provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        upstream_request = replace(
            request,
            provider_id=self._upstream_capabilities.provider_id,
            provider_kind=self._upstream_capabilities.kind,
            fallback_provider_ids=(),
        )
        try:
            response = await self._provider.complete(upstream_request)
        except asyncio.CancelledError:
            raise
        except ModelGatewayError as error:
            # Do not retain provider-controlled diagnostics as a public cause or
            # context chain. The outer ModelGateway will normalize this typed
            # route-bound error to its canonical safe public message.
            raise ModelGatewayError(
                error.code,
                "routed model provider failed",
                provider_id=self._route_id,
                retryable=error.retryable,
                failure_effect=error.failure_effect,
            ) from None

        if response.request_id != request.request_id:
            raise _route_error("routed provider returned a response for another request")
        if response.provider_id != self._upstream_capabilities.provider_id:
            raise _route_error("routed provider returned an unexpected provider identity")
        if response.provider_kind is not self._upstream_capabilities.kind:
            raise _route_error("routed provider returned an unexpected provider kind")

        return replace(response, provider_id=self._route_id)

    def __repr__(self) -> str:
        return (
            f"RoutedModelProvider(route_id={self._route_id!r}, "
            f"upstream_provider_id={self._upstream_capabilities.provider_id!r})"
        )


def _route_error(message: str) -> ModelGatewayError:
    return ModelGatewayError(
        ModelErrorCode.PROVIDER_ERROR,
        message,
        retryable=False,
        failure_effect=ModelFailureEffect.UNKNOWN,
    )


def _validate_route_id(value: str) -> None:
    if type(value) is not str:
        raise TypeError("route_id must be text")
    if not value or value != value.strip():
        raise ValueError("route_id must be non-empty canonical text")
    if len(value) > MAX_PROVIDER_ROUTE_ID_CHARS:
        raise ValueError(f"route_id must contain at most {MAX_PROVIDER_ROUTE_ID_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("route_id must not contain control characters")


def _validate_upstream_provider_id(value: str) -> None:
    if type(value) is not str:
        raise TypeError("upstream provider_id must be text")
    if not value or value != value.strip():
        raise ValueError("upstream provider_id must be non-empty canonical text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("upstream provider_id must not contain control characters")
