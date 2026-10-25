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
    ModelUsage,
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
        if type(request.request_id) is not str:
            raise _route_error(
                self._route_id,
                "routed request identity must be exact text",
            )
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
            try:
                self._require_stable_capabilities()
            except Exception:  # noqa: BLE001 - untrusted capability boundary
                terminal_error = _route_error(
                    self._route_id,
                    "routed provider capabilities changed during failed execution",
                )
            else:
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
        snapshot = _snapshot_response(response, route_id=self._route_id)
        if snapshot.request_id != request.request_id:
            raise _route_error(
                self._route_id,
                "routed provider returned a response for another request",
            )
        if snapshot.provider_id != self._upstream_capabilities.provider_id:
            raise _route_error(
                self._route_id,
                "routed provider returned an unexpected provider identity",
            )
        if snapshot.provider_kind is not self._upstream_capabilities.kind:
            raise _route_error(
                self._route_id,
                "routed provider returned an unexpected provider kind",
            )

        return ModelResponse(
            request_id=snapshot.request_id,
            text=snapshot.text,
            provider_id=self._route_id,
            provider_kind=snapshot.provider_kind,
            model=snapshot.model,
            usage=snapshot.usage,
            latency_ms=snapshot.latency_ms,
        )

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

    # Copy first, then validate only the Nika-owned exact dataclass snapshot. This
    # avoids validating one provider-owned state and later re-reading a different
    # state while still preserving future canonical capability fields.
    snapshot = replace(value)
    _validate_upstream_provider_id(snapshot.provider_id)
    if type(snapshot.kind) is not ProviderKind:
        raise TypeError("routed provider kind must be ProviderKind")
    for name, flag in (
        ("supports_private_data", snapshot.supports_private_data),
        ("supports_tools", snapshot.supports_tools),
        ("supports_streaming", snapshot.supports_streaming),
        ("supports_hard_cancellation", snapshot.supports_hard_cancellation),
    ):
        if type(flag) is not bool:
            raise TypeError(f"routed provider {name} must be bool")
    return snapshot


def _snapshot_response(value: object, *, route_id: str) -> ModelResponse:
    if type(value) is not ModelResponse:
        raise _route_error(route_id, "routed provider returned a malformed response")

    # Snapshot the exact outer DTO before validating it, then copy nested usage as
    # its own exact Nika-owned DTO. Subsequent checks/reprojection never re-read
    # the provider-owned response object.
    snapshot = replace(value)
    if type(snapshot.usage) is not ModelUsage:
        raise _route_error(route_id, "routed provider returned malformed usage")
    usage = replace(snapshot.usage)

    for label, item in (
        ("request_id", snapshot.request_id),
        ("text", snapshot.text),
        ("provider_id", snapshot.provider_id),
        ("model", snapshot.model),
    ):
        if type(item) is not str:
            raise _route_error(
                route_id,
                f"routed provider returned malformed {label}",
            )
    if type(snapshot.provider_kind) is not ProviderKind:
        raise _route_error(route_id, "routed provider returned malformed provider kind")

    token_values = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    for token_value in token_values:
        if token_value is not None and type(token_value) is not int:
            raise _route_error(route_id, "routed provider returned malformed usage")

    latency_ms = snapshot.latency_ms
    if latency_ms is not None and type(latency_ms) not in {int, float}:
        raise _route_error(route_id, "routed provider returned malformed latency")

    return ModelResponse(
        request_id=snapshot.request_id,
        text=snapshot.text,
        provider_id=snapshot.provider_id,
        provider_kind=snapshot.provider_kind,
        model=snapshot.model,
        usage=usage,
        latency_ms=latency_ms,
    )


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
