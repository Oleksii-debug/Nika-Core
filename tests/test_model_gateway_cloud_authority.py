from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.security.model_cloud_authority import (
    StandingPermissionCloudEffectAuthorizer,
    StandingPermissionExecutionAuthority,
)
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
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
            effect_network_host="api.example.test",
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


class _LocalProvider:
    def __init__(self) -> None:
        self.complete_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="local-provider",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="local response",
            provider_id="local-provider",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "local-model",
        )


class _CountingCredentialResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == "env:NIKA_QA_CLOUD_KEY"
        self.calls += 1
        return "synthetic-cloud-authority-secret"


class _CountingTransport:
    def __init__(self, *, text: str = "authorized production response") -> None:
        self.calls = 0
        self._text = text

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": self._text}}],
            },
        )


class _EncodedAs(str):
    """Visible text with a different hash input under legacy isinstance(str) checks."""

    def __new__(cls, visible: str, encoded_as: str) -> Self:
        instance = super().__new__(cls, visible)
        instance._encoded_as = encoded_as
        return instance

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        return self._encoded_as.encode(encoding, errors)


def _request(*, request_id: str = "cloud-request-1") -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="fixture"),),
        model="model-a",
        provider_id="approved-api",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def _local_request() -> ModelRequest:
    return ModelRequest(
        request_id="local-request-1",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="local-model",
        provider_id="local-provider",
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def _authority(
    tmp_path: Path,
    *,
    now: datetime,
) -> tuple[
    StandingPermissionStore,
    StandingPermissionBinding,
    StandingPermissionExecutionAuthority,
]:
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
    execution_authority = StandingPermissionExecutionAuthority(
        subject_id="agent-1",
        context=context,
    )
    return permissions, binding, execution_authority


def _authorizer(
    permissions: StandingPermissionStore,
    binding: StandingPermissionBinding,
    execution_authority: StandingPermissionExecutionAuthority,
    *,
    at: datetime,
) -> StandingPermissionCloudEffectAuthorizer:
    return StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda candidate: binding if candidate == execution_authority else None,
        clock=lambda: at,
    )


async def _complete_with_authority(
    gateway: ModelGateway,
    authorizer: StandingPermissionCloudEffectAuthorizer,
    authority: StandingPermissionExecutionAuthority,
    request: ModelRequest,
) -> ModelResponse:
    with authorizer.execution_scope(authority):
        return await gateway.complete(request)


def _standing_use(
    binding: StandingPermissionBinding,
    *,
    request_id: str,
) -> StandingPermissionUse:
    intent = ActionIntent(
        action_id=request_id,
        tool_id="model.cloud.complete",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target=binding.target,
        network_host=binding.network_host,
        task_id=binding.context.task_id,
        project_id=binding.context.project_id,
        site=binding.network_host,
        resource=binding.resource_id,
        arguments={"request_id": request_id},
        effect_id=request_id,
    )
    return StandingPermissionUse(
        subject_id=binding.subject_id,
        context=binding.context,
        intent=intent,
        resource_id=binding.resource_id,
    )


def _production_provider(
    resolver: _CountingCredentialResolver,
    transport: _CountingTransport,
    *,
    base_url: str = "https://api.example.test/v1",
) -> CredentialRefOpenAICompatibleProvider:
    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
            timeout=timeout,
        )

    return CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="approved-api",
            base_url=base_url,
            default_model="model-a",
            credential_ref="env:NIKA_QA_CLOUD_KEY",
        ),
        credential_resolver=resolver,
        client_factory=client_factory,
    )


def test_cloud_route_without_authorizer_fails_before_provider_effect() -> None:
    provider = _CloudProvider()
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.provider_id == "approved-api"
    assert provider.complete_calls == 0


def test_cloud_route_requires_current_host_execution_scope(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=now)
    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=now + timedelta(seconds=1),
    )
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.complete_calls == 0


def test_current_standing_permission_admits_exact_cloud_execution(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=now)
    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=now + timedelta(seconds=1),
    )
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    response = asyncio.run(
        _complete_with_authority(gateway, authorizer, authority, _request())
    )

    assert response.provider_id == "approved-api"
    assert response.model == "model-a"
    assert provider.complete_calls == 1


def test_revoked_standing_permission_blocks_cloud_provider_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=now)
    permissions.revoke(
        binding.permission_id,
        revoked_at=now + timedelta(seconds=2),
    )
    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=now + timedelta(seconds=3),
    )
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _complete_with_authority(gateway, authorizer, authority, _request())
        )

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.complete_calls == 0


def test_shared_gateway_cannot_spend_task_a_binding_for_task_b(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority_a = _authority(tmp_path, now=now)
    authority_b = StandingPermissionExecutionAuthority(
        subject_id=authority_a.subject_id,
        context=PermissionContext(
            user_id="user-1",
            project_id="project-1",
            task_id="task-2",
        ),
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda _authority: binding,
        clock=lambda: now + timedelta(seconds=1),
    )
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _complete_with_authority(gateway, authorizer, authority_b, _request())
        )

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.complete_calls == 0

    response = asyncio.run(
        _complete_with_authority(gateway, authorizer, authority_a, _request())
    )
    assert response.provider_id == "approved-api"
    assert provider.complete_calls == 1


def test_execution_scope_is_not_inherited_as_authority_by_child_task(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=now)
    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=now + timedelta(seconds=1),
    )
    provider = _CloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    async def run() -> None:
        with authorizer.execution_scope(authority):
            child = asyncio.create_task(gateway.complete(_request()))
            with pytest.raises(ModelGatewayError) as caught:
                await child
            assert caught.value.code is ModelErrorCode.INVALID_REQUEST
            assert provider.complete_calls == 0

            response = await gateway.complete(_request())
            assert response.provider_id == "approved-api"

    asyncio.run(run())
    assert provider.complete_calls == 1


def test_local_route_bypasses_cloud_authority(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, _binding, _authority_value = _authority(tmp_path, now=now)

    def forbidden_resolver(
        _authority: StandingPermissionExecutionAuthority,
    ) -> StandingPermissionBinding | None:
        raise AssertionError("LOCAL route must not resolve CLOUD authority")

    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        forbidden_resolver,
        clock=lambda: now + timedelta(seconds=1),
    )
    provider = _LocalProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    response = asyncio.run(gateway.complete(_local_request()))

    assert response.provider_id == "local-provider"
    assert provider.complete_calls == 1


def test_production_cloud_path_uses_current_authority_before_credentials(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=now)
    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=now + timedelta(seconds=1),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(_production_provider(resolver, transport))

    response = asyncio.run(
        _complete_with_authority(gateway, authorizer, authority, _request())
    )

    assert response.text == "authorized production response"
    assert resolver.calls == 1
    assert transport.calls == 1


def test_revocation_blocks_production_credentials_and_http_transport(
    tmp_path: Path,
) -> None:
    planned_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=planned_at)
    request = _request(request_id="revoked-production-request")

    permissions.authorize(
        binding.permission_id,
        _standing_use(binding, request_id=request.request_id),
        now=planned_at + timedelta(seconds=1),
    )
    permissions.revoke(
        binding.permission_id,
        revoked_at=planned_at + timedelta(seconds=2),
    )
    with pytest.raises(PermissionError, match="revoked"):
        permissions.authorize(
            binding.permission_id,
            _standing_use(binding, request_id=request.request_id),
            now=planned_at + timedelta(seconds=3),
        )

    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=planned_at + timedelta(seconds=3),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport(text="must not execute")
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(_production_provider(resolver, transport))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _complete_with_authority(gateway, authorizer, authority, request)
        )

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.provider_id == "approved-api"
    assert resolver.calls == 0
    assert transport.calls == 0


def test_forged_task_text_subclass_cannot_spoof_durable_scope_before_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    stored_context = PermissionContext(
        user_id="user-1",
        project_id="project-1",
        task_id="task-1",
    )
    binding_context = PermissionContext(
        user_id="user-1",
        project_id="project-1",
        task_id="task-2",
    )
    permissions = StandingPermissionStore(SQLiteStore(tmp_path / "task-alias.sqlite3"))
    permissions.initialize()
    permissions.grant(
        permission_id="cloud-model-permission",
        scope=StandingPermissionScope(
            subject_id="agent-1",
            context=stored_context,
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
        context=binding_context,
        target="approved-api",
        resource_id="model-a",
        network_host="api.example.test",
    )

    forged_context = object.__new__(PermissionContext)
    object.__setattr__(forged_context, "user_id", "user-1")
    object.__setattr__(forged_context, "project_id", "project-1")
    object.__setattr__(forged_context, "task_id", _EncodedAs("task-2", "task-1"))
    forged_authority = object.__new__(StandingPermissionExecutionAuthority)
    object.__setattr__(forged_authority, "subject_id", "agent-1")
    object.__setattr__(forged_authority, "context", forged_context)

    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda _authority: binding,
        clock=lambda: now + timedelta(seconds=1),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport(text="must not execute")
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(_production_provider(resolver, transport))

    with pytest.raises(TypeError, match="context.task_id must be exact text"):
        asyncio.run(
            _complete_with_authority(
                gateway,
                authorizer,
                forged_authority,
                _request(request_id="forged-task-authority"),
            )
        )

    assert resolver.calls == 0
    assert transport.calls == 0


def test_binding_resource_text_subclass_cannot_alias_store_hash_before_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    context = PermissionContext(
        user_id="user-1",
        project_id="project-1",
        task_id="task-1",
    )
    permissions = StandingPermissionStore(SQLiteStore(tmp_path / "resource-alias.sqlite3"))
    permissions.initialize()
    permissions.grant(
        permission_id="cloud-model-permission",
        scope=StandingPermissionScope(
            subject_id="agent-1",
            context=context,
            action_class="model.cloud.complete",
            targets=("approved-api",),
            sites=("api.example.test",),
            resources=("stored-model",),
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
        resource_id=_EncodedAs("model-a", "stored-model"),
        network_host="api.example.test",
    )
    authority = StandingPermissionExecutionAuthority(
        subject_id="agent-1",
        context=context,
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda _authority: binding,
        clock=lambda: now + timedelta(seconds=1),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport(text="must not execute")
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(_production_provider(resolver, transport))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _complete_with_authority(
                gateway,
                authorizer,
                authority,
                _request(request_id="resource-alias-authority"),
            )
        )

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert resolver.calls == 0
    assert transport.calls == 0


def test_production_effect_host_must_match_standing_site_before_credentials(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    permissions, binding, authority = _authority(tmp_path, now=now)
    authorizer = _authorizer(
        permissions,
        binding,
        authority,
        at=now + timedelta(seconds=1),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport(text="must not execute")
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(
        _production_provider(
            resolver,
            transport,
            base_url="https://other.example.test/v1",
        )
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _complete_with_authority(
                gateway,
                authorizer,
                authority,
                _request(request_id="wrong-effect-host"),
            )
        )

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.provider_id == "approved-api"
    assert resolver.calls == 0
    assert transport.calls == 0