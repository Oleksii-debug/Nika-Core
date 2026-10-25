from __future__ import annotations

from dataclasses import fields, replace

from nika_core.model_gateway.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.route import RoutedModelProvider


class _CapabilityProvider:
    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self._capabilities = capabilities

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"capability forwarding proof must not execute: {request.request_id}")


def _capabilities_with_every_known_trusted_field() -> ProviderCapabilities:
    kwargs: dict[str, object] = {
        "provider_id": "upstream-cloud",
        "kind": ProviderKind.CLOUD,
        "supports_private_data": True,
        "supports_tools": True,
        "supports_streaming": True,
        "supports_hard_cancellation": True,
    }
    field_names = {field.name for field in fields(ProviderCapabilities)}
    # #704 adds this trusted authority field. Keeping this conditional makes the
    # exact same regression useful both before and after that serialized contract
    # lands: once present, a non-default host must survive outer route wrapping.
    if "effect_network_host" in field_names:
        kwargs["effect_network_host"] = "api.example.test"
    return ProviderCapabilities(**kwargs)  # type: ignore[arg-type]


def test_routed_provider_preserves_every_trusted_capability_field_except_identity() -> None:
    upstream = _capabilities_with_every_known_trusted_field()
    route = RoutedModelProvider(
        route_id="account-a-route",
        provider=_CapabilityProvider(upstream),
    )

    assert route.upstream_provider_id == "upstream-cloud"
    assert route.capabilities == replace(upstream, provider_id="account-a-route")
