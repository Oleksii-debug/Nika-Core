from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.model_gateway.contracts import (
    ModelRequest,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

_CLOUD_ACTION_CLASS = "model.cloud.complete"


@dataclass(frozen=True, slots=True)
class StandingPermissionExecutionAuthority:
    """Trusted host identity for one ModelGateway execution lineage."""

    subject_id: str
    context: PermissionContext

    def __post_init__(self) -> None:
        if type(self.subject_id) is not str:
            raise TypeError("subject_id must be exact text")
        if type(self.context) is not PermissionContext:
            raise TypeError("context must be an exact PermissionContext")


@dataclass(frozen=True, slots=True)
class _ExecutionScope:
    authority: StandingPermissionExecutionAuthority
    task: asyncio.Task[object]


BindingResolver = Callable[
    [StandingPermissionExecutionAuthority],
    StandingPermissionBinding | None,
]


class StandingPermissionCloudEffectAuthorizer:
    """Revalidate per-execution standing authority at CLOUD effect admission."""

    def __init__(
        self,
        permissions: StandingPermissionStore,
        binding_resolver: BindingResolver,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(binding_resolver):
            raise TypeError("binding_resolver must be callable")
        self._permissions = permissions
        self._binding_resolver = binding_resolver
        self._clock = clock or (lambda: datetime.now(UTC))
        self._execution_scope: ContextVar[_ExecutionScope | None] = ContextVar(
            f"nika_cloud_execution_authority_{id(self)}",
            default=None,
        )

    @contextmanager
    def execution_scope(
        self,
        authority: StandingPermissionExecutionAuthority,
    ) -> Iterator[None]:
        """Bind trusted authority to exactly the current async execution task."""

        if type(authority) is not StandingPermissionExecutionAuthority:
            raise TypeError("authority must be exact StandingPermissionExecutionAuthority")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("cloud execution authority requires a running async task")
        token = self._execution_scope.set(_ExecutionScope(authority=authority, task=task))
        try:
            yield
        finally:
            self._execution_scope.reset(token)

    def authorize_cloud_effect(
        self,
        *,
        request: ModelRequest,
        provider: ProviderCapabilities,
    ) -> None:
        authority = self._current_execution_authority()
        try:
            binding = self._binding_resolver(authority)
        except Exception:  # noqa: BLE001 - resolver is a host integration boundary
            raise PermissionError("cloud execution authority could not be resolved") from None
        if type(binding) is not StandingPermissionBinding:
            raise PermissionError("cloud execution authority has no standing binding")
        if not self._binding_matches_execution(binding, authority):
            raise PermissionError("standing permission does not belong to this execution")
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
            task_id=authority.context.task_id,
            project_id=authority.context.project_id,
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
                subject_id=authority.subject_id,
                context=authority.context,
                intent=intent,
                resource_id=binding.resource_id,
            ),
            now=self._clock(),
        )

    def _current_execution_authority(self) -> StandingPermissionExecutionAuthority:
        scope = self._execution_scope.get()
        task = asyncio.current_task()
        if scope is None or task is None or scope.task is not task:
            raise PermissionError("cloud execution has no trusted host authority")
        return scope.authority

    @staticmethod
    def _binding_matches_execution(
        binding: StandingPermissionBinding,
        authority: StandingPermissionExecutionAuthority,
    ) -> bool:
        context = authority.context
        binding_context = binding.context
        return (
            binding.subject_id == authority.subject_id
            and type(binding_context) is PermissionContext
            and binding_context.user_id == context.user_id
            and binding_context.project_id == context.project_id
            and binding_context.task_id == context.task_id
        )
