from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from nika_core.config import AppConfig
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
    CredentialResolutionError,
    EnvironmentCredentialResolver,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


_CANARY = "synthetic-worker48-secret-do-not-log"
_CREDENTIAL_REF = "env:NIKA_WORKER48_API_KEY"


class _StaticResolver:
    def __init__(self, material: str) -> None:
        self._material = material

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == _CREDENTIAL_REF
        return self._material


class _ExplodingResolver:
    def resolve(self, credential_ref: str) -> str:
        raise RuntimeError(f"resolver accidentally mentioned {_CANARY} for {credential_ref}")


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.events.append(
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": payload or {},
            }
        )
        return len(self.events)


class _LocalProvider:
    def __init__(self) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="ollama",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text="local ok",
            provider_id="ollama",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "qwen3:8b",
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


class _UnavailableCloudProvider:
    def __init__(self) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="offline-api",
            kind=ProviderKind.CLOUD,
            supports_private_data=False,
        )
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "configured model provider is unavailable",
            provider_id=self.capabilities.provider_id,
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


def _config(**overrides: object) -> ApiModelRouteConfig:
    values: dict[str, object] = {
        "provider_id": "approved-api",
        "base_url": "https://api.example.test/v1",
        "default_model": "model-a",
        "credential_ref": _CREDENTIAL_REF,
        "supports_private_data": False,
    }
    values.update(overrides)
    return ApiModelRouteConfig(**values)  # type: ignore[arg-type]


def _request(
    *,
    request_id: str = "request-1",
    provider_id: str = "approved-api",
    model: str = "model-a",
    privacy: PrivacyClass = PrivacyClass.PUBLIC,
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(
            ModelMessage(role="system", content="Work as one V0.1 agent."),
            ModelMessage(role="user", content="Return a short deterministic fixture result."),
        ),
        model=model,
        provider_id=provider_id,
        privacy=privacy,
        timeout_seconds=2.0,
    )


def _client_factory(handler: httpx.MockTransport):
    def factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=handler, timeout=timeout)

    return factory


def test_environment_credential_resolver_requires_reference_and_never_embeds_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = EnvironmentCredentialResolver()
    monkeypatch.setenv("NIKA_WORKER48_API_KEY", _CANARY)

    assert resolver.resolve(_CREDENTIAL_REF) == _CANARY

    with pytest.raises(CredentialResolutionError):
        resolver.resolve("literal-secret")
    with pytest.raises(CredentialResolutionError):
        resolver.resolve("env:")


def test_api_route_config_is_secret_free_and_rejects_unsafe_urls() -> None:
    config = _config()
    durable_state = {
        "provider_id": config.provider_id,
        "base_url": config.base_url,
        "default_model": config.default_model,
        "credential_ref": config.credential_ref,
    }

    serialized = json.dumps(durable_state, sort_keys=True)
    assert _CANARY not in serialized
    assert _CREDENTIAL_REF in serialized
    assert _CREDENTIAL_REF not in repr(config)

    with pytest.raises(ValueError, match="HTTPS"):
        _config(base_url="http://api.example.test/v1")
    with pytest.raises(ValueError, match="userinfo"):
        _config(base_url="https://user:password@api.example.test/v1")
    with pytest.raises(ValueError, match="query or fragment"):
        _config(base_url="https://api.example.test/v1?token=bad")


def test_api_route_resolves_credential_only_at_execution_and_redacts_audit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "api ok"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    audit = _Audit()
    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    request = _request()
    response = asyncio.run(gateway.complete(request))

    assert response.text == "api ok"
    assert response.provider_id == "approved-api"
    assert response.provider_kind is ProviderKind.CLOUD
    assert response.model == "model-a"
    assert len(seen) == 1
    sent = seen[0]
    assert str(sent.url) == "https://api.example.test/v1/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {_CANARY}"
    assert _CANARY not in repr(audit.events)
    assert _CREDENTIAL_REF not in repr(audit.events)
    assert any(
        event["event_type"] == "model.completed"
        and event["payload"].get("model") == "model-a"  # type: ignore[union-attr]
        for event in audit.events
    )


def test_credential_resolution_failure_is_normalized_without_secret_or_cause() -> None:
    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_ExplodingResolver(),
    )
    gateway = ModelGateway()
    gateway.register(provider)
    request = _request()
    before = request

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(request))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.provider_id == "approved-api"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert error.__cause__ is None
    assert _CANARY not in str(error)
    assert _CREDENTIAL_REF not in str(error)
    assert request == before


def test_http_auth_failure_drops_secret_bearing_transport_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway()
    gateway.register(api)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.__cause__ is None
    assert _CANARY not in str(error)
    assert _CANARY not in repr(error)


def test_api_timeout_is_normalized_and_does_not_cross_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    local = _LocalProvider()
    api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway()
    gateway.register(local)
    gateway.register(api)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert caught.value.provider_id == "approved-api"
    assert local.requests == []


def test_local_and_api_routes_use_same_semantic_request_shape_without_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "api ok"}}],
            },
        )

    local = _LocalProvider()
    api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway()
    gateway.register(local)
    gateway.register(api)

    local_request = _request(provider_id="ollama")
    api_request = replace(local_request, request_id="request-api", provider_id="approved-api")

    local_response = asyncio.run(gateway.complete(local_request))
    api_response = asyncio.run(gateway.complete(api_request))

    assert local_response.provider_kind is ProviderKind.LOCAL
    assert api_response.provider_kind is ProviderKind.CLOUD
    assert local_request.messages == api_request.messages
    assert local_request.model == api_request.model
    assert local_request.timeout_seconds == api_request.timeout_seconds
    assert local_request.fallback_provider_ids == api_request.fallback_provider_ids == ()


@pytest.mark.parametrize(
    ("selected_provider", "selected_model", "updated_provider", "updated_model"),
    (
        ("ollama", "qwen3:8b", "approved-api", "model-a"),
        ("approved-api", "model-a", "ollama", "qwen3:8b"),
    ),
)
def test_running_task_route_survives_default_change_and_restart(
    selected_provider: str,
    selected_model: str,
    updated_provider: str,
    updated_model: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": {"content": "api ok"}}],
            },
        )

    audit = _Audit()
    local = _LocalProvider()
    api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(local)
    gateway.register(api)

    settings = AppConfig(model_provider=selected_provider)
    running_request = _request(
        request_id="route-running",
        provider_id=settings.model_provider,
        model=selected_model,
    )
    durable_route = json.dumps(
        {"provider_id": running_request.provider_id, "model": running_request.model},
        sort_keys=True,
    )

    settings.model_provider = updated_provider
    running_response = asyncio.run(gateway.complete(running_request))
    new_response = asyncio.run(
        gateway.complete(
            _request(
                request_id="route-new",
                provider_id=settings.model_provider,
                model=updated_model,
            )
        )
    )

    assert running_response.provider_id == selected_provider
    assert running_response.model == selected_model
    assert new_response.provider_id == updated_provider
    assert new_response.model == updated_model
    assert running_request.provider_id == selected_provider
    assert running_request.model == selected_model
    assert running_request.fallback_provider_ids == ()
    assert _CANARY not in durable_route
    assert _CREDENTIAL_REF not in durable_route
    assert api.credential_ref == _CREDENTIAL_REF
    assert all(event["event_type"] != "model.fallback" for event in audit.events)

    completed = [
        event["payload"]
        for event in audit.events
        if event["event_type"] == "model.completed"
    ]
    assert [
        (payload["provider_id"], payload["model"])  # type: ignore[index]
        for payload in completed
    ] == [(selected_provider, selected_model), (updated_provider, updated_model)]
    assert _CANARY not in repr(audit.events)
    assert _CREDENTIAL_REF not in repr(audit.events)

    restored = json.loads(durable_route)
    restart_audit = _Audit()
    restart_gateway = ModelGateway(audit_log=restart_audit)
    restart_gateway.register(_LocalProvider())
    restart_api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    restart_gateway.register(restart_api)
    restored_request = _request(
        request_id="route-restarted",
        provider_id=restored["provider_id"],
        model=restored["model"],
    )

    restarted_response = asyncio.run(restart_gateway.complete(restored_request))

    assert restarted_response.provider_id == selected_provider
    assert restarted_response.model == selected_model
    assert all(event["event_type"] != "model.fallback" for event in restart_audit.events)
    assert any(
        event["event_type"] == "model.completed"
        and event["payload"].get("provider_id") == selected_provider  # type: ignore[union-attr]
        and event["payload"].get("model") == selected_model  # type: ignore[union-attr]
        for event in restart_audit.events
    )
    assert _CANARY not in repr(restart_audit.events)
    assert _CREDENTIAL_REF not in repr(restart_audit.events)


def test_unavailable_declared_provider_never_switches_route_and_keeps_safe_identity() -> None:
    audit = _Audit()
    local = _LocalProvider()
    unavailable = _UnavailableCloudProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(local)
    gateway.register(unavailable)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            gateway.complete(
                _request(
                    request_id="route-unavailable",
                    provider_id="offline-api",
                    model="offline-model",
                )
            )
        )

    error = caught.value
    assert error.code is ModelErrorCode.UNAVAILABLE
    assert error.provider_id == "offline-api"
    assert unavailable.requests
    assert local.requests == []
    assert all(event["event_type"] != "model.fallback" for event in audit.events)
    assert any(
        event["event_type"] == "model.failed"
        and event["payload"].get("provider_id") == "offline-api"  # type: ignore[union-attr]
        and event["payload"].get("model") == "offline-model"  # type: ignore[union-attr]
        for event in audit.events
    )
    assert _CANARY not in repr(audit.events)
    assert _CREDENTIAL_REF not in repr(audit.events)


def test_api_route_cancellation_propagates_and_does_not_launch_other_route() -> None:
    entered = asyncio.Event()
    audit = _Audit()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={})

    local = _LocalProvider()
    api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(local)
    gateway.register(api)

    async def scenario() -> None:
        task = asyncio.create_task(gateway.complete(_request()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert local.requests == []
    assert any(event["event_type"] == "model.cancelled" for event in audit.events)
    assert _CANARY not in repr(audit.events)


def test_three_agent_calls_share_one_provider_neutral_model_request_contract() -> None:
    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        received.append(payload["messages"][-1]["content"])
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "agent ok"}}],
            },
        )

    api = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(_CANARY),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway()
    gateway.register(api)

    roles = ("planner", "worker", "checker")
    responses = tuple(
        asyncio.run(
            gateway.complete(
                ModelRequest(
                    request_id=f"v01-{role}",
                    messages=(
                        ModelMessage(role="system", content=f"You are the {role} agent."),
                        ModelMessage(role="user", content="same V0.1 task"),
                    ),
                    model="model-a",
                    provider_id="approved-api",
                    privacy=PrivacyClass.PUBLIC,
                    timeout_seconds=2.0,
                )
            )
        )
        for role in roles
    )

    assert tuple(response.provider_id for response in responses) == (
        "approved-api",
        "approved-api",
        "approved-api",
    )
    assert received == ["same V0.1 task"] * 3
