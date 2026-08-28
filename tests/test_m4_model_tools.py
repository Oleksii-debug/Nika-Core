from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mcp.server import MCPServer

from nika_core.mcp_boundary import MCPClientAdapter, MCPServerConfig
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import (
    DeterministicMockProvider,
    OpenAICompatibleProvider,
)
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


def test_openai_compatible_provider_runs_same_gateway_contract() -> None:
    seen: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = http_request.read().decode("utf-8")
        seen["authorization"] = http_request.headers.get("authorization")
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "model": "controlled-model",
                "choices": [{"message": {"content": "provider: hello"}}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 2,
                    "total_tokens": 4,
                },
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    gateway = ModelGateway()
    gateway.register(
        OpenAICompatibleProvider(
            provider_id="controlled-http",
            base_url="https://provider.invalid/v1",
            kind=ProviderKind.CLOUD,
            default_model="controlled-model",
            api_key="runtime-only-test-key",
            supports_private_data=True,
            client_factory=client_factory,
        ),
        default=True,
    )

    response = asyncio.run(
        gateway.complete(
            request(provider_kind=ProviderKind.CLOUD, privacy=PrivacyClass.PRIVATE)
        )
    )

    assert response.text == "provider: hello"
    assert response.provider_id == "controlled-http"
    assert response.provider_kind is ProviderKind.CLOUD
    assert response.usage.total_tokens == 4
    assert seen["authorization"] == "Bearer runtime-only-test-key"
    assert "hello" in str(seen["body"])


def test_http_provider_failure_maps_to_typed_gateway_error() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(429, json={"error": "busy"})
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    gateway = ModelGateway()
    gateway.register(
        OpenAICompatibleProvider(
            provider_id="rate-limited",
            base_url="https://provider.invalid/v1",
            kind=ProviderKind.CLOUD,
            default_model="controlled-model",
            client_factory=client_factory,
        ),
        default=True,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request(provider_kind=ProviderKind.CLOUD)))

    assert exc_info.value.code is ModelErrorCode.RATE_LIMITED
    assert exc_info.value.retryable is True


def test_gateway_timeout_is_typed() -> None:
    class SlowProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            await asyncio.sleep(0.05)
            return await super().complete(model_request)

    gateway = ModelGateway()
    gateway.register(SlowProvider(), default=True)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request(timeout_seconds=0.001)))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT


def test_gateway_cancellation_propagates() -> None:
    class SlowProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            await asyncio.sleep(60)
            return await super().complete(model_request)

    async def scenario() -> None:
        gateway = ModelGateway()
        gateway.register(SlowProvider(), default=True)
        task = asyncio.create_task(gateway.complete(request()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


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


def test_gateway_uses_explicit_fallback_after_retryable_failure() -> None:
    calls: list[str] = []

    class BusyProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            calls.append("primary")
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "temporarily unavailable",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    class FallbackProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            calls.append("fallback")
            return await super().complete(model_request)

    gateway = ModelGateway()
    gateway.register(BusyProvider(provider_id="primary"))
    gateway.register(FallbackProvider(provider_id="fallback"))

    response = asyncio.run(
        gateway.complete(
            request(
                provider_kind=None,
                provider_id="primary",
                fallback_provider_ids=("fallback",),
            )
        )
    )

    assert response.provider_id == "fallback"
    assert calls == ["primary", "fallback"]


def test_gateway_does_not_fallback_after_non_retryable_failure() -> None:
    fallback_called = False

    class RejectedProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            raise ModelGatewayError(
                ModelErrorCode.AUTHENTICATION,
                "credentials rejected",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

    class FallbackProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            nonlocal fallback_called
            fallback_called = True
            return await super().complete(model_request)

    gateway = ModelGateway()
    gateway.register(RejectedProvider(provider_id="primary"))
    gateway.register(FallbackProvider(provider_id="fallback"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                request(
                    provider_kind=None,
                    provider_id="primary",
                    fallback_provider_ids=("fallback",),
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.AUTHENTICATION
    assert fallback_called is False


def test_gateway_does_not_fallback_after_timeout_without_hard_cancellation() -> None:
    fallback_called = False

    class NonCancellableProvider(DeterministicMockProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider_id="non-cancellable",
                kind=ProviderKind.LOCAL,
                supports_private_data=True,
                supports_hard_cancellation=False,
            )

        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            await asyncio.sleep(0.05)
            return await super().complete(model_request)

    class FallbackProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            nonlocal fallback_called
            fallback_called = True
            return await super().complete(model_request)

    gateway = ModelGateway()
    gateway.register(NonCancellableProvider(provider_id="non-cancellable"))
    gateway.register(FallbackProvider(provider_id="fallback"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                request(
                    provider_kind=None,
                    provider_id="non-cancellable",
                    fallback_provider_ids=("fallback",),
                    timeout_seconds=0.001,
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.retryable is False
    assert fallback_called is False


def test_sensitive_fallback_route_is_rejected_before_primary_execution() -> None:
    primary_called = False

    class PrimaryProvider(DeterministicMockProvider):
        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            nonlocal primary_called
            primary_called = True
            return await super().complete(model_request)

    class UnsafeCloudProvider(DeterministicMockProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider_id="unsafe-cloud",
                kind=ProviderKind.CLOUD,
                supports_private_data=False,
            )

    gateway = ModelGateway()
    gateway.register(PrimaryProvider(provider_id="primary"))
    gateway.register(UnsafeCloudProvider(provider_id="unsafe-cloud"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                request(
                    provider_kind=None,
                    provider_id="primary",
                    fallback_provider_ids=("unsafe-cloud",),
                    privacy=PrivacyClass.SENSITIVE,
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.provider_id == "unsafe-cloud"
    assert primary_called is False


def test_model_request_rejects_invalid_fallback_routes() -> None:
    with pytest.raises(ValueError, match="unique"):
        request(fallback_provider_ids=("same", "same"))

    with pytest.raises(ValueError, match="primary provider"):
        request(
            provider_kind=None,
            provider_id="same",
            fallback_provider_ids=("same",),
        )


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

    result = asyncio.run(
        executor.execute(ToolCall(call_id="call-3", tool_id="slow", arguments={}))
    )

    assert not result.ok
    assert result.error == "tool timed out"


def test_mcp_official_sdk_in_process_discovery_and_call() -> None:
    server = MCPServer("nika-m4-test")
    approval_calls = 0

    @server.tool()
    async def add(left: int, right: int) -> dict[str, int]:
        """Add two integers."""
        return {"sum": left + right}

    async def approve(spec: ToolSpec, call: ToolCall) -> bool:
        nonlocal approval_calls
        approval_calls += 1
        assert spec.risk is ToolRisk.EXTERNAL_SIDE_EFFECT
        assert call.tool_id == "mcp:test:add"
        return True

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="test", target=server),
        approval_policy=approve,
    )

    specs = asyncio.run(adapter.list_tools())
    assert len(specs) == 1
    assert specs[0].tool_id == "mcp:test:add"
    assert specs[0].risk is ToolRisk.EXTERNAL_SIDE_EFFECT
    assert specs[0].input_schema["type"] == "object"

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="mcp-call-1",
                tool_id="mcp:test:add",
                arguments={"left": 2, "right": 3},
                approved=True,
            )
        )
    )

    assert result.ok
    assert result.output == {"sum": 5}
    assert approval_calls == 1
