from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway import contracts
from nika_core.model_gateway.gateway import ModelGateway

_CANARY = "QA53_SYNTHETIC_DEV17_PROVIDER_SECRET_7f2c9b1e"


class _TypedFailureProvider:
    def __init__(self, *, include_provider_id: bool) -> None:
        self._include_provider_id = include_provider_id
        self._capabilities = contracts.ProviderCapabilities(
            provider_id="qa53-provider",
            kind=contracts.ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> contracts.ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: contracts.ModelRequest) -> contracts.ModelResponse:
        del request
        raise contracts.ModelGatewayError(
            contracts.ModelErrorCode.AUTHENTICATION,
            f"Authorization: Bearer {_CANARY}",
            provider_id=(self.capabilities.provider_id if self._include_provider_id else None),
            retryable=False,
        )


def _request() -> contracts.ModelRequest:
    return contracts.ModelRequest(
        request_id="qa53-dev17-provider-error",
        messages=(contracts.ModelMessage(role="user", content="synthetic public payload"),),
        provider_id="qa53-provider",
        privacy=contracts.PrivacyClass.PUBLIC,
        timeout_seconds=1.0,
    )


@pytest.mark.parametrize("include_provider_id", [False, True])
def test_provider_secret_cannot_escape_public_gateway_exception(
    include_provider_id: bool,
) -> None:
    """QA_ONLY oracle: provider diagnostic secrets must not escape via str/repr.

    DEV17 currently preserves a typed provider's message in two paths:
    * provider_id=None -> _normalize_provider_error rebuilds using str(error)
    * matching provider_id -> the raw ModelGatewayError is re-raised unchanged

    The canary is synthetic. This test intentionally fails on the exact DEV17
    candidate until the owner normalizes the public exception boundary.
    """

    gateway = ModelGateway()
    gateway.register(_TypedFailureProvider(include_provider_id=include_provider_id))

    with pytest.raises(contracts.ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request()))

    escaped = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert _CANARY not in escaped
