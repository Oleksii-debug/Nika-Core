from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import DeterministicMockProvider
from nika_core.tools import ToolCall, ToolExecutor, ToolRisk, ToolSpec


def request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "req-1",
        "messages": (ModelMessage(role="user", content="hello"),),
        "provider_kind": ProviderKind.NO_LLM,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def test_mock_scenario_runs_through_gateway() -> None:
    gateway = ModelGateway()
    gateway.register(DeterministicMockProvider(), default=True)

    response = asyncio.run(gateway.complete(request()))

    assert response.text == "mock: hello"
    assert response.provider_id == "mock"
    assert response.provider_kind is ProviderKind.NO_LLM


def test_gateway_requires_explicit_route_with_multiple_providers() -> None:
    gateway = ModelGateway()
    gateway.register(DeterministicMockProvider(provider_id="first"))
    gateway.register(DeterministicMockProvider(provider_id="second"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request(provider_kind=None)))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST


def test_sensitive_data_fails_closed_for_untrusted_provider() -> None:
    class UnsafeProvider(DeterministicMockProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider_id="unsafe",
                kind=ProviderKind.CLOUD,
                supports_private_data=False,
            )

    gateway = ModelGateway()
    gateway.register(UnsafeProvider(provider_id="unsafe"), default=True)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                request(
                    provider_kind=ProviderKind.CLOUD,
                    privacy=PrivacyClass.SENSITIVE,
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST


def test_dangerous_tool_requires_approval() -> None:
    called = False

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return arguments

    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="publish",
            description="publish externally",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        handler,
    )

    result = asyncio.run(
        executor.execute(ToolCall(call_id="call-1", tool_id="publish", arguments={"x": 1}))
    )

    assert not result.ok
    assert result.error == "approval required"
    assert called is False


def test_approved_tool_executes_and_returns_standard_result() -> None:
    async def handler(arguments: dict[str, object]) -> object:
        return {"echo": arguments["value"]}

    executor = ToolExecutor()
    executor.register(ToolSpec(tool_id="echo", description="echo"), handler)

    result = asyncio.run(
        executor.execute(
            ToolCall(call_id="call-2", tool_id="echo", arguments={"value": "ok"})
        )
    )

    assert result.ok
    assert result.output == {"echo": "ok"}


def test_tool_timeout_is_normalized() -> None:
    async def handler(_arguments: dict[str, object]) -> object:
        await asyncio.sleep(0.05)
        return "late"

    executor = ToolExecutor()
    executor.register(
        ToolSpec(tool_id="slow", description="slow", timeout_seconds=0.001), handler
    )

    result = asyncio.run(executor.execute(ToolCall(call_id="call-3", tool_id="slow", arguments={})))

    assert not result.ok
    assert result.error == "tool timed out"
