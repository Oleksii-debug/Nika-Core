from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRoutePolicy,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderCostClass,
    ProviderKind,
    ProviderResourceClass,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import (
    DeterministicMockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
)


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "route-1",
        "messages": (ModelMessage(role="user", content="payload-sentinel"),),
        "provider_kind": None,
        "provider_id": "primary",
        "privacy": PrivacyClass.PUBLIC,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


class _CapabilityProvider(DeterministicMockProvider):
    def __init__(
        self,
        *,
        provider_id: str,
        kind: ProviderKind,
        supports_private_data: bool,
        cost_class: ProviderCostClass | None,
        resource_class: ProviderResourceClass | None,
    ) -> None:
        super().__init__(provider_id=provider_id)
        self._test_capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
            cost_class=cost_class,
            resource_class=resource_class,
        )
        self.called = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._test_capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.called = True
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "test-model",
        )


def test_private_route_is_validated_end_to_end_before_primary_receives_payload() -> None:
    primary = _CapabilityProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    unsafe_fallback = _CapabilityProvider(
        provider_id="unsafe-cloud",
        kind=ProviderKind.CLOUD,
        supports_private_data=False,
        cost_class=ProviderCostClass.METERED,
        resource_class=ProviderResourceClass.REMOTE_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(unsafe_fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(
                    privacy=PrivacyClass.PRIVATE,
                    fallback_provider_ids=("unsafe-cloud",),
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.POLICY_DENIED
    assert exc_info.value.provider_id == "unsafe-cloud"
    assert primary.called is False
    assert unsafe_fallback.called is False


def test_local_only_route_rejects_cloud_fallback_before_primary_execution() -> None:
    primary = _CapabilityProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    cloud = _CapabilityProvider(
        provider_id="cloud",
        kind=ProviderKind.CLOUD,
        supports_private_data=True,
        cost_class=ProviderCostClass.METERED,
        resource_class=ProviderResourceClass.REMOTE_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(cloud)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(
                    fallback_provider_ids=("cloud",),
                    route_policy=ModelRoutePolicy(local_only=True),
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.POLICY_DENIED
    assert exc_info.value.provider_id == "cloud"
    assert primary.called is False


def test_no_metered_route_fails_closed_on_metered_or_unknown_cost() -> None:
    primary = _CapabilityProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    unknown_cost = _CapabilityProvider(
        provider_id="unknown-cost",
        kind=ProviderKind.CLOUD,
        supports_private_data=True,
        cost_class=None,
        resource_class=ProviderResourceClass.REMOTE_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(unknown_cost)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(
                    fallback_provider_ids=("unknown-cost",),
                    route_policy=ModelRoutePolicy(allow_metered=False),
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.POLICY_DENIED
    assert primary.called is False


def test_resource_class_policy_fails_closed_before_any_provider_call() -> None:
    primary = _CapabilityProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    remote = _CapabilityProvider(
        provider_id="remote",
        kind=ProviderKind.CLOUD,
        supports_private_data=True,
        cost_class=ProviderCostClass.METERED,
        resource_class=ProviderResourceClass.REMOTE_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(remote)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(
                    fallback_provider_ids=("remote",),
                    route_policy=ModelRoutePolicy(
                        allowed_resource_classes=frozenset(
                            {ProviderResourceClass.LOCAL_SERVICE}
                        )
                    ),
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.RESOURCE_LIMIT
    assert primary.called is False


@pytest.mark.parametrize(
    "code",
    [
        ModelErrorCode.AUTHENTICATION,
        ModelErrorCode.POLICY_DENIED,
        ModelErrorCode.RESOURCE_LIMIT,
        ModelErrorCode.INVALID_REQUEST,
        ModelErrorCode.CANCELLED,
    ],
)
def test_terminal_typed_failures_never_fallback_even_if_provider_marks_retryable(
    code: ModelErrorCode,
) -> None:
    fallback = _CapabilityProvider(
        provider_id="fallback",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )

    class BrokenProvider(_CapabilityProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise ModelGatewayError(
                code,
                "must fail closed",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    primary = BrokenProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request(fallback_provider_ids=("fallback",))))

    assert exc_info.value.code is code
    assert fallback.called is False


def test_task_cancellation_never_silently_falls_through_to_fallback() -> None:
    fallback = _CapabilityProvider(
        provider_id="fallback",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )

    class BlockingProvider(_CapabilityProvider):
        def __init__(self) -> None:
            super().__init__(
                provider_id="primary",
                kind=ProviderKind.LOCAL,
                supports_private_data=True,
                cost_class=ProviderCostClass.LOCAL_RESOURCE,
                resource_class=ProviderResourceClass.LOCAL_SERVICE,
            )
            self.started = asyncio.Event()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.started.set()
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    async def scenario() -> None:
        primary = BlockingProvider()
        gateway = ModelGateway()
        gateway.register(primary)
        gateway.register(fallback)
        task = asyncio.create_task(
            gateway.complete(_request(fallback_provider_ids=("fallback",)))
        )
        await primary.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert fallback.called is False


def test_fallback_attempt_receives_only_remaining_total_deadline() -> None:
    seen_timeout: list[float] = []

    class RetryablePrimary(_CapabilityProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            await asyncio.sleep(0.02)
            raise ModelGatewayError(
                ModelErrorCode.RATE_LIMITED,
                "busy",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    class RecordingFallback(_CapabilityProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            seen_timeout.append(request.timeout_seconds)
            return await super().complete(request)

    primary = RetryablePrimary(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    fallback = RecordingFallback(
        provider_id="fallback",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    response = asyncio.run(
        gateway.complete(
            _request(timeout_seconds=0.5, fallback_provider_ids=("fallback",))
        )
    )

    assert response.provider_id == "fallback"
    assert len(seen_timeout) == 1
    assert 0 < seen_timeout[0] < 0.5


def test_untyped_provider_failure_is_normalized_and_never_falls_back() -> None:
    fallback = _CapabilityProvider(
        provider_id="fallback",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )

    class UntypedFailure(_CapabilityProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("provider-specific failure")

    primary = UntypedFailure(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request(fallback_provider_ids=("fallback",))))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.retryable is False
    assert fallback.called is False


@pytest.mark.parametrize("field", ["request_id", "provider_id", "provider_kind"])
def test_gateway_rejects_normalized_response_identity_drift(field: str) -> None:
    class DriftProvider(_CapabilityProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            values: dict[str, object] = {
                "request_id": request.request_id,
                "text": "answer",
                "provider_id": self.capabilities.provider_id,
                "provider_kind": self.capabilities.kind,
                "model": "test-model",
            }
            if field == "request_id":
                values[field] = "other-request"
            elif field == "provider_id":
                values[field] = "other-provider"
            else:
                values[field] = ProviderKind.CLOUD
            return ModelResponse(**values)  # type: ignore[arg-type]

    provider = DriftProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.retryable is False


def test_gateway_rejects_non_model_response_from_adapter() -> None:
    class InvalidProvider(_CapabilityProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            return object()  # type: ignore[return-value]

    provider = InvalidProvider(
        provider_id="primary",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, ModelErrorCode.AUTHENTICATION),
        (403, ModelErrorCode.POLICY_DENIED),
        (413, ModelErrorCode.RESOURCE_LIMIT),
    ],
)
def test_http_auth_policy_and_resource_failures_fail_closed(
    status: int, expected_code: ModelErrorCode
) -> None:
    fallback = _CapabilityProvider(
        provider_id="fallback",
        kind=ProviderKind.LOCAL,
        supports_private_data=True,
        cost_class=ProviderCostClass.LOCAL_RESOURCE,
        resource_class=ProviderResourceClass.LOCAL_SERVICE,
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, json={"error": "rejected"})
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OpenAICompatibleProvider(
        provider_id="primary",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="controlled-model",
        supports_private_data=True,
        client_factory=client_factory,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(
                    fallback_provider_ids=("fallback",),
                    privacy=PrivacyClass.PRIVATE,
                )
            )
        )

    assert exc_info.value.code is expected_code
    assert exc_info.value.retryable is False
    assert fallback.called is False


def test_openai_compatible_rejects_malformed_text_and_usage() -> None:
    bodies = [
        {
            "model": "controlled-model",
            "choices": [{"message": {"content": ""}}],
        },
        {
            "model": "controlled-model",
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": True},
        },
    ]

    for body in bodies:
        transport = httpx.MockTransport(
            lambda _request, response_body=body: httpx.Response(200, json=response_body)
        )

        def client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, **kwargs)

        provider = OpenAICompatibleProvider(
            provider_id="provider",
            base_url="https://provider.invalid/v1",
            kind=ProviderKind.CLOUD,
            default_model="controlled-model",
            supports_private_data=True,
            client_factory=client_factory,
        )

        with pytest.raises(ModelGatewayError) as exc_info:
            asyncio.run(
                provider.complete(
                    _request(provider_id="provider", privacy=PrivacyClass.PRIVATE)
                )
            )

        assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
        assert exc_info.value.retryable is False


def test_model_usage_and_response_reject_malformed_normalized_values() -> None:
    with pytest.raises(TypeError, match="input_tokens"):
        ModelUsage(input_tokens=True)
    with pytest.raises(ValueError, match="output_tokens"):
        ModelUsage(output_tokens=-1)
    with pytest.raises(ValueError, match="total_tokens"):
        ModelUsage(input_tokens=5, total_tokens=4)
    with pytest.raises(ValueError, match="response text"):
        ModelResponse(
            request_id="request",
            text=" ",
            provider_id="provider",
            provider_kind=ProviderKind.LOCAL,
            model="model",
        )


def test_provider_registry_exposes_cost_and_resource_metadata_without_provider_types() -> None:
    gateway = ModelGateway()
    gateway.register(
        OpenAICompatibleProvider(
            provider_id="local-openai",
            base_url="http://127.0.0.1:8000/v1",
            kind=ProviderKind.LOCAL,
            default_model="local-model",
            supports_private_data=True,
        )
    )
    gateway.register(OllamaProvider(default_model="qwen3:8b"))

    capabilities = {item.provider_id: item for item in gateway.provider_capabilities()}

    assert capabilities["local-openai"].cost_class is ProviderCostClass.LOCAL_RESOURCE
    assert capabilities["local-openai"].resource_class is ProviderResourceClass.LOCAL_SERVICE
    assert capabilities["ollama"].cost_class is ProviderCostClass.LOCAL_RESOURCE
    assert capabilities["ollama"].resource_class is ProviderResourceClass.LOCAL_SERVICE


def test_registering_two_defaults_for_one_kind_fails_closed() -> None:
    gateway = ModelGateway()
    gateway.register(DeterministicMockProvider(provider_id="first"), default=True)

    with pytest.raises(ValueError, match="default provider already registered"):
        gateway.register(DeterministicMockProvider(provider_id="second"), default=True)


def test_audit_contains_routing_metadata_but_not_prompt_or_request_metadata() -> None:
    class RecordingAudit:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def append(self, **event: object) -> int:
            self.events.append(event)
            return len(self.events)

    audit = RecordingAudit()
    gateway = ModelGateway(audit_log=audit)  # type: ignore[arg-type]
    gateway.register(DeterministicMockProvider(provider_id="primary"))

    response = asyncio.run(
        gateway.complete(
            _request(
                messages=(ModelMessage(role="user", content="PROMPT_CONTENT_SENTINEL"),),
                metadata={"credential_reference": "SECRET_METADATA_SENTINEL"},
            )
        )
    )

    assert response.provider_id == "primary"
    serialized = repr(audit.events)
    assert "PROMPT_CONTENT_SENTINEL" not in serialized
    assert "SECRET_METADATA_SENTINEL" not in serialized
    assert "cost_class" in serialized
    assert "resource_class" in serialized
