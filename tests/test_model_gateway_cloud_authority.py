from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
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
from nika_core.security.model_cloud_authority import StandingPermissionCloudEffectAuthorizer
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk


class _CloudProvider:
    def __init__(self) -> None:
        self.complete_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="approved-api",
            kind=ProviderKind.CLOUD,
            supports_private_data=False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="authorized cloud response",
            provider_id="approved-api",
            provider_kind=ProviderKind.CLOUD,
            model=request.model or "model-a",
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="cloud-request-1",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="model-a",
        provider_id="approved-api",
        provider_kind=ProviderKind.CLOUD,
        timeout_seconds=2.0,
    )


def _authority(
    tmp_path: Path,
    *,
    now: datetime,
) -> tuple[StandingPermissionStore, StandingPermissionBinding, StandingPermissionCloudEffectAuthorizer]:
    context = PermissionContext(
        user_id="user-1",
        project_id="project-1",
        task_id="task-1",
    )
    permissions = StandingPermissionStore(SQLiteStore(tmp_path / "authority.sqlite3"))
    permissions.initialize()
    permissions.grant(
        permission_id="cloud-model-permission",
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
        permission_id="cloud-model-permission",
        subject_id="agent-1",
        context=context,
        target="approved-api",
        resource_id="model-a",
        network_host="api.example.test",
    )
    return (
        permissions,
        binding,
        StandingPermissionCloudEffectAuthorizer(
            permissions,
            binding,
            clock=lambda: now + timedelta(seconds=1),
        ),
    )


def test_cloud_route_without_current_authority_fails_before_provider_effect() -> None:
    provider = _CloudProvider()
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.provider_id == "approved-api"
    assert provider.complete_calls == 0


def test_current_standing_permission_admits_exact_cloud_provider_and_model(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 13, 11, 0, tzinfo=UTC)
    _permissions, _binding, authorizer = _authority(tmp_path, now=now)
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    response = asyncio.run(gateway.complete(_request()))

    assert response.provider_id == "approved-api"
    assert response.model == "model-a"
    assert provider.complete_calls == 1


def test_revoked_standing_permission_blocks_cloud_provider_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 13, 11, 0, tzinfo=UTC)
    permissions, binding, _authorizer = _authority(tmp_path, now=now)
    permissions.revoke(
        binding.permission_id,
        revoked_at=now + timedelta(seconds=2),
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        binding,
        clock=lambda: now + timedelta(seconds=3),
    )
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.provider_id == "approved-api"
    assert provider.complete_calls == 0
