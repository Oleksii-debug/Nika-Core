from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.security.model_cloud_authority import (
    StandingPermissionCloudEffectAuthorizer,
    StandingPermissionExecutionAuthority,
)
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk


class _RecordingResolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return "synthetic-route-snapshot-secret"


class _TextSubclass(str):
    pass


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="route-snapshot-request",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="model-a",
        provider_id="approved-api",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def _client_factory(
    transport: httpx.MockTransport,
):
    def factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    return factory


def test_provider_uses_constructor_route_snapshot_after_caller_mutation() -> None:
    resolver = _RecordingResolver()
    transport_calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        assert request.url.host == "api.example.test"
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "snapshot response"}}],
            },
        )

    config = ApiModelRouteConfig(
        provider_id="approved-api",
        base_url="https://api.example.test/v1",
        default_model="model-a",
        credential_ref="env:NIKA_ROUTE_A",
        supports_private_data=False,
        supports_hard_cancellation=False,
    )
    provider = CredentialRefOpenAICompatibleProvider(
        config=config,
        credential_resolver=resolver,
        client_factory=_client_factory(httpx.MockTransport(transport)),
    )

    object.__setattr__(config, "provider_id", "substituted-api")
    object.__setattr__(config, "base_url", "https://other.example.test/v1")
    object.__setattr__(config, "default_model", "model-b")
    object.__setattr__(config, "credential_ref", "env:NIKA_ROUTE_B")
    object.__setattr__(config, "supports_private_data", True)
    object.__setattr__(config, "supports_hard_cancellation", True)

    capabilities = provider.capabilities
    assert capabilities.provider_id == "approved-api"
    assert capabilities.effect_network_host == "api.example.test"
    assert capabilities.supports_private_data is False
    assert capabilities.supports_hard_cancellation is False
    assert provider.credential_ref == "env:NIKA_ROUTE_A"

    response = asyncio.run(provider.complete(_request()))

    assert response.provider_id == "approved-api"
    assert response.model == "model-a"
    assert response.text == "snapshot response"
    assert resolver.references == ["env:NIKA_ROUTE_A"]
    assert transport_calls == 1


def test_registered_cloud_route_keeps_original_credential_after_config_mutation(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    context = PermissionContext(
        user_id="user-1",
        project_id="project-1",
        task_id="task-1",
    )
    permissions = StandingPermissionStore(SQLiteStore(tmp_path / "route-snapshot.sqlite3"))
    permissions.initialize()
    permissions.grant(
        permission_id="route-snapshot-permission",
        scope=StandingPermissionScope(
            subject_id="agent-1",
            context=context,
            action_class="model.cloud.complete",
            targets=("approved-api",),
            sites=("api.example.test",),
            resources=("model-a",),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=now,
            expires_at=now + timedelta(hours=1),
        ),
    )
    binding = StandingPermissionBinding(
        permission_id="route-snapshot-permission",
        subject_id="agent-1",
        context=context,
        target="approved-api",
        resource_id="model-a",
        network_host="api.example.test",
    )
    authority = StandingPermissionExecutionAuthority(
        subject_id="agent-1",
        context=context,
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda candidate: binding if candidate == authority else None,
        clock=lambda: now + timedelta(seconds=1),
    )

    resolver = _RecordingResolver()
    transport_calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        assert request.url.host == "api.example.test"
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "authorized snapshot"}}],
            },
        )

    config = ApiModelRouteConfig(
        provider_id="approved-api",
        base_url="https://api.example.test/v1",
        default_model="model-a",
        credential_ref="env:NIKA_ROUTE_A",
    )
    provider = CredentialRefOpenAICompatibleProvider(
        config=config,
        credential_resolver=resolver,
        client_factory=_client_factory(httpx.MockTransport(transport)),
    )
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    object.__setattr__(config, "credential_ref", "env:NIKA_ROUTE_B")

    async def run() -> None:
        with authorizer.execution_scope(authority):
            response = await gateway.complete(_request())
        assert response.text == "authorized snapshot"

    asyncio.run(run())

    assert resolver.references == ["env:NIKA_ROUTE_A"]
    assert provider.credential_ref == "env:NIKA_ROUTE_A"
    assert transport_calls == 1


def test_provider_rejects_behavioral_route_identity_scalar() -> None:
    config = ApiModelRouteConfig(
        provider_id=_TextSubclass("approved-api"),
        base_url="https://api.example.test/v1",
        default_model="model-a",
        credential_ref="env:NIKA_ROUTE_A",
    )

    with pytest.raises(TypeError, match="provider_id must be exact text"):
        CredentialRefOpenAICompatibleProvider(
            config=config,
            credential_resolver=_RecordingResolver(),
        )
