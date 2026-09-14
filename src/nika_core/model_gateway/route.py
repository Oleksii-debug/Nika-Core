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
    ProviderKind,
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
        capabilities = _canonical_capabilities(provider.capabilities)
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
        self._require_stable_capabilities()
        upstream_request = replace(
            request,
            provider_id=self._upstream_capabilities.provider_id,
            provider_kind=self._upstream_capabilities.kind,
            fallback_provider_ids=(),
        )
        terminal_error: ModelGatewayError | None = None
        response: ModelResponse | None = None
        try:
            response = await self._provider.complete(upstream_request)
        except asyncio.CancelledError:
            raise
        except ModelGatewayError as error:
            # A typed upstream error is authoritative only while the provider's
            # registered capability identity is still unchanged *after* the
            # attempted effect. Otherwise optimistic retryable/NO_EFFECT truth
            # could be laundered through a route whose execution surface drifted
            # during complete(). Capability access is itself an untrusted provider
            # boundary, so any malformed/drifting/throwing observation discards
            # that optimistic authority and becomes route-scoped UNKNOWN.
            try:
                self._require_stable_capabilities()
            except Exception:  # noqa: BLE001 - untrusted capability boundary
                terminal_error = _route_error(
                    self._route_id,
                    "routed provider capabilities changed during failed execution",
                )
            else:
                # Snapshot only canonical typed truth. Provider-controlled text and
                # malformed/foreign error authority are never retained.
                terminal_error = _rebind_upstream_error(
                    route_id=self._route_id,
                    upstream_provider_id=self._upstream_capabilities.provider_id,
                    error=error,
                )

        # Raise outside the provider exception handler so Python does not attach
        # provider-controlled diagnostics through __context__.
        if terminal_error is not None:
            raise terminal_error
        if response is None:
            raise _route_error(
                self._route_id,
                "routed provider completed without a response",
            )

        self._require_stable_capabilities()
        if response.request_id != request.request_id:
            raise _route_error(
                self._route_id,
                "routed provider returned a response for another request",
            )
        if response.provider_id != self._upstream_capabilities.provider_id:
            raise _route_error(
                self._route_id,
                "routed provider returned an unexpected provider identity",
            )
        if response.provider_kind is not self._upstream_capabilities.kind:
            raise _route_error(
                self._route_id,
                "routed provider returned an unexpected provider kind",
            )

        return replace(response, provider_id=self._route_id)

    def _require_stable_capabilities(self) -> None:
        observed = _canonical_capabilities(self._provider.capabilities)
        if observed != self._upstream_capabilities:
            raise _route_error(
                self._route_id,
                "routed provider capabilities changed after registration",
            )

    def __repr__(self) -> str:
        return (
            f"RoutedModelProvider(route_id={self._route_id!r}, "
            f"upstream_provider_id={self._upstream_capabilities.provider_id!r})"
        )


def _canonical_capabilities(value: ProviderCapabilities) -> ProviderCapabilities:
    if type(value) is not ProviderCapabilities:
        raise TypeError("routed provider capabilities must be ProviderCapabilities")
    _validate_upstream_provider_id(value.provider_id)
    if type(value.kind) is not ProviderKind:
        raise TypeError("routed provider kind must be ProviderKind")
    for name, flag in (
        ("supports_private_data", value.supports_private_data),
        ("supports_tools", value.supports_tools),
        ("supports_streaming", value.supports_streaming),
        ("supports_hard_cancellation", value.supports_hard_cancellation),
    ):
        if type(flag) is not bool:
            raise TypeError(f"routed provider {name} must be bool")

    # Preserve the exact canonical capability DTO rather than reconstructing it
    # field-by-field. That keeps route identity orthogonal to capability growth:
    # when the canonical contract adds trusted fields (for example CLOUD effect
    # host authority), they survive the wrapper instead of silently resetting to
    # a dataclass default. ModelGateway remains the final canonical validator for
    # the registered outer route.
    return replace(value)


def _rebind_upstream_error(
    *,
    route_id: str,
    upstream_provider_id: str,
    error: ModelGatewayError,
) -> ModelGatewayError:
    if type(error) is not ModelGatewayError:
        return _route_error(route_id, "routed provider returned a malformed error")
    if type(error.code) is not ModelErrorCode:
        return _route_error(route_id, "routed provider returned a malformed error")
    if type(error.retryable) is not bool:
        return _route_error(route_id, "routed provider returned a malformed error")
    if type(error.failure_effect) is not ModelFailureEffect:
        return _route_error(route_id, "routed provider returned a malformed error")
    if error.provider_id is not None and (
        type(error.provider_id) is not str or error.provider_id != upstream_provider_id
    ):
        return _route_error(
            route_id,
            "routed provider returned an error for another provider identity",
        )
    return ModelGatewayError(
        error.code,
        "routed model provider failed",
        provider_id=route_id,
        retryable=error.retryable,
        failure_effect=error.failure_effect,
    )


def _route_error(route_id: str, message: str) -> ModelGatewayError:
    return ModelGatewayError(
        ModelErrorCode.PROVIDER_ERROR,
        message,
        provider_id=route_id,
        retryable=False,
        failure_effect=ModelFailureEffect.UNKNOWN,
    )


def _validate_route_id(value: str) -> None:
    if type(value) is not str:
        raise TypeError("route_id must be text")
    if not value or value != value.strip():
        raise ValueError("route_id must be non-empty canonical text")
    if len(value) > MAX_PROVIDER_ROUTE_ID_CHARS:
        raise ValueError(
            f"route_id must contain at most {MAX_PROVIDER_ROUTE_ID_CHARS} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("route_id must not contain control characters")


def _validate_upstream_provider_id(value: str) -> None:
    if type(value) is not str:
        raise TypeError("upstream provider_id must be text")
    if not value or value != value.strip():
        raise ValueError("upstream provider_id must be non-empty canonical text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("upstream provider_id must not contain control characters")
