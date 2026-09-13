from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from nika_core.model_gateway.contracts import (
    ModelRequest,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    StandingPermissionBinding,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

_CLOUD_ACTION_CLASS = "model.cloud.complete"


class StandingPermissionCloudEffectAuthorizer:
    """Revalidate canonical standing authority at ModelGateway CLOUD effect admission."""

    def __init__(
        self,
        permissions: StandingPermissionStore,
        binding: StandingPermissionBinding,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._permissions = permissions
        self._binding = binding
        self._clock = clock or (lambda: datetime.now(UTC))

    def authorize_cloud_effect(
        self,
        *,
        request: ModelRequest,
        provider: ProviderCapabilities,
    ) -> None:
        binding = self._binding
        if provider.kind is not ProviderKind.CLOUD:
            raise PermissionError(
                "standing cloud authority cannot authorize a non-cloud provider"
            )
        if provider.provider_id != binding.target or request.provider_id != binding.target:
            raise PermissionError("cloud provider is outside standing permission scope")
        if request.model is None or request.model != binding.resource_id:
            raise PermissionError("cloud model is outside standing permission scope")

        intent = ActionIntent(
            action_id=request.request_id,
            tool_id=_CLOUD_ACTION_CLASS,
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            target=binding.target,
            network_host=binding.network_host,
            task_id=binding.context.task_id,
            project_id=binding.context.project_id,
            site=binding.network_host,
            resource=binding.resource_id,
            arguments={
                "request_id": request.request_id,
                "provider_id": provider.provider_id,
                "model": request.model,
            },
            effect_id=request.request_id,
        )
        self._permissions.authorize(
            binding.permission_id,
            StandingPermissionUse(
                subject_id=binding.subject_id,
                context=binding.context,
                intent=intent,
                resource_id=binding.resource_id,
            ),
            now=self._clock(),
        )
