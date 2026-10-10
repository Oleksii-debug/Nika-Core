from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk


class _CountingCredentialResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == "env:NIKA_QA_CLOUD_KEY"
        self.calls += 1
        return "synthetic-one-shot-39-secret"


def _cloud_use(*, request_id: str, context: PermissionContext) -> StandingPermissionUse:
    intent = ActionIntent(
        action_id=request_id,
        tool_id="model.cloud.complete",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target="approved-api",
        network_host="api.example.test",
        task_id=context.task_id,
        project_id=context.project_id,
        site="api.example.test",
        resource="model-a",
        arguments={"request_id": request_id},
        effect_id=request_id,
    )
    return StandingPermissionUse(
        subject_id="agent-1",
        context=context,
        intent=intent,
        resource_id="model-a",
    )


def test_cloud_execution_revalidates_revoked_authority_before_credentials_or_transport(
    tmp_path,
) -> None:
    """A planning-time allow must not survive revocation into provider execution."""

    planned_at = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)
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
            granted_at=planned_at,
            expires_at=planned_at + timedelta(hours=1),
        ),
    )
    use = _cloud_use(request_id="request-1", context=context)

    # Planning sees valid authority. This must not become durable execution authority.
    permissions.authorize(
        "cloud-model-permission",
        use,
        now=planned_at + timedelta(seconds=1),
    )
    permissions.revoke(
        "cloud-model-permission",
        revoked_at=planned_at + timedelta(seconds=2),
    )

    # Canonical security state already observes the revoke before execution starts.
    with pytest.raises(PermissionError, match="revoked"):
        permissions.authorize(
            "cloud-model-permission",
            use,
            now=planned_at + timedelta(seconds=3),
        )

    resolver = _CountingCredentialResolver()
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "must not execute"}}],
            },
        )

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )

    provider = CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="approved-api",
            base_url="https://api.example.test/v1",
            default_model="model-a",
            credential_ref="env:NIKA_QA_CLOUD_KEY",
        ),
        credential_resolver=resolver,
        client_factory=client_factory,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    request = ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="deterministic QA fixture"),),
        model="model-a",
        provider_id="approved-api",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )

    # Error taxonomy is deliberately not prescribed by this QA oracle. The invariant is
    # that current authority must deny before secret resolution or any cloud transport.
    try:
        asyncio.run(gateway.complete(request))
    except (ModelGatewayError, PermissionError):
        pass

    assert resolver.calls == 0, "revoked authority reached credential resolution"
    assert transport_calls == 0, "revoked authority reached cloud transport"
