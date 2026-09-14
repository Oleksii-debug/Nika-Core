from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.parallel import complete_parallel
from nika_core.model_gateway.route import RoutedModelProvider


@dataclass
class _Overlap:
    active: int = 0
    max_active: int = 0


class _ReplicaProvider:
    def __init__(
        self,
        *,
        overlap: _Overlap,
        reached: asyncio.Event,
        release: asyncio.Event,
        provider_id: str = "ollama",
        model: str = "same-model",
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_tools=False,
            supports_streaming=False,
            supports_hard_cancellation=False,
        )
        self._overlap = overlap
        self._reached = reached
        self._release = release
        self._model = model
        self.observed_provider_ids: list[str | None] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.observed_provider_ids.append(request.provider_id)
        self._overlap.active += 1
        self._overlap.max_active = max(self._overlap.max_active, self._overlap.active)
        if self._overlap.active >= 2:
            self._reached.set()
        try:
            await self._release.wait()
        finally:
            self._overlap.active -= 1
        return ModelResponse(
            request_id=request.request_id,
            text=f"ok:{request.request_id}",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or self._model,
        )


class _ErrorProvider:
    def __init__(self) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="upstream-api",
            kind=ProviderKind.CLOUD,
            supports_private_data=False,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelGatewayError(
            ModelErrorCode.RATE_LIMITED,
            "SECRET-UPSTREAM-DIAGNOSTIC",
            provider_id="upstream-api",
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


class _MismatchedResponseProvider:
    def __init__(self) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="ollama",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text="wrong route identity",
            provider_id="foreign-provider",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "m",
        )


class _DriftingProvider:
    def __init__(self) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="stable-before",
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self._capabilities = ProviderCapabilities(
            provider_id="changed-after-effect",
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )
        return ModelResponse(
            request_id=request.request_id,
            text="must not escape as route success",
            provider_id="stable-before",
            provider_kind=ProviderKind.CLOUD,
            model=request.model or "m",
        )


def _request(request_id: str, *, route_id: str, model: str = "same-model") -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="route identity proof"),),
        provider_id=route_id,
        model=model,
        timeout_seconds=5.0,
    )


def test_two_same_provider_model_replicas_overlap_with_distinct_route_ids() -> None:
    async def scenario() -> tuple[int, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        overlap = _Overlap()
        reached = asyncio.Event()
        release = asyncio.Event()
        replica_a = _ReplicaProvider(overlap=overlap, reached=reached, release=release)
        replica_b = _ReplicaProvider(overlap=overlap, reached=reached, release=release)
        gateway = ModelGateway()
        gateway.register(RoutedModelProvider(route_id="ollama-replica-a", provider=replica_a))
        gateway.register(RoutedModelProvider(route_id="ollama-replica-b", provider=replica_b))

        batch = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request("request-a", route_id="ollama-replica-a"),
                    _request("request-b", route_id="ollama-replica-b"),
                ),
                max_parallel=2,
                provider_limits={"ollama-replica-a": 1, "ollama-replica-b": 1},
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=2.0)
        observed = overlap.max_active
        release.set()
        result = await asyncio.wait_for(batch, timeout=2.0)
        route_ids = tuple(
            outcome.response.provider_id
            for outcome in result.completed
            if outcome.response is not None
        )
        models = tuple(
            outcome.response.model
            for outcome in result.completed
            if outcome.response is not None
        )
        upstream_ids = tuple(replica_a.observed_provider_ids + replica_b.observed_provider_ids)
        return observed, route_ids, models, upstream_ids

    observed, route_ids, models, upstream_ids = asyncio.run(scenario())

    assert observed == 2
    assert route_ids == ("ollama-replica-a", "ollama-replica-b")
    assert models == ("same-model", "same-model")
    assert upstream_ids == ("ollama", "ollama")


def test_route_capabilities_preserve_provider_truth_except_outer_identity() -> None:
    overlap = _Overlap()
    provider = _ReplicaProvider(
        overlap=overlap,
        reached=asyncio.Event(),
        release=asyncio.Event(),
        provider_id="upstream",
    )
    route = RoutedModelProvider(route_id="route-a", provider=provider)

    assert route.route_id == "route-a"
    assert route.upstream_provider_id == "upstream"
    assert route.capabilities == ProviderCapabilities(
        provider_id="route-a",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        supports_tools=False,
        supports_streaming=False,
        supports_hard_cancellation=False,
    )


def test_typed_upstream_failure_is_route_bound_and_diagnostic_is_not_retained() -> None:
    async def scenario() -> ModelGatewayError:
        gateway = ModelGateway()
        gateway.register(RoutedModelProvider(route_id="api-route-a", provider=_ErrorProvider()))
        with pytest.raises(ModelGatewayError) as captured:
            await gateway.complete(_request("request", route_id="api-route-a", model="api-model"))
        return captured.value

    error = asyncio.run(scenario())

    assert error.code is ModelErrorCode.RATE_LIMITED
    assert error.provider_id == "api-route-a"
    assert error.retryable is True
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert "SECRET-UPSTREAM-DIAGNOSTIC" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_mismatched_upstream_provider_identity_fails_closed_as_route_error() -> None:
    async def scenario() -> ModelGatewayError:
        gateway = ModelGateway()
        gateway.register(
            RoutedModelProvider(
                route_id="ollama-route",
                provider=_MismatchedResponseProvider(),
            )
        )
        with pytest.raises(ModelGatewayError) as captured:
            await gateway.complete(_request("request", route_id="ollama-route"))
        return captured.value

    error = asyncio.run(scenario())

    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "ollama-route"
    assert error.failure_effect is ModelFailureEffect.UNKNOWN


def test_provider_capability_drift_after_effect_cannot_become_route_success() -> None:
    async def scenario() -> tuple[ModelGatewayError, int]:
        provider = _DriftingProvider()
        gateway = ModelGateway()
        gateway.register(RoutedModelProvider(route_id="stable-route", provider=provider))
        with pytest.raises(ModelGatewayError) as captured:
            await gateway.complete(_request("request", route_id="stable-route", model="m"))
        return captured.value, provider.calls

    error, calls = asyncio.run(scenario())

    assert calls == 1
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "stable-route"
    assert error.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.parametrize(
    "route_id",
    ["", " route", "route ", "line\nbreak", "x" * 129],
)
def test_route_id_must_be_bounded_canonical_text(route_id: str) -> None:
    overlap = _Overlap()
    provider = _ReplicaProvider(
        overlap=overlap,
        reached=asyncio.Event(),
        release=asyncio.Event(),
    )
    with pytest.raises(ValueError):
        RoutedModelProvider(route_id=route_id, provider=provider)


def test_route_id_rejects_string_subclass_before_registration() -> None:
    class _HostileText(str):
        pass

    overlap = _Overlap()
    provider = _ReplicaProvider(
        overlap=overlap,
        reached=asyncio.Event(),
        release=asyncio.Event(),
    )
    with pytest.raises(TypeError, match="route_id must be text"):
        RoutedModelProvider(route_id=_HostileText("route"), provider=provider)
