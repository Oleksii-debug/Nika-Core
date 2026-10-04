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
        subject_id = _exact_text(self.subject_id, "subject_id")
        context = _snapshot_permission_context(self.context)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "context", context)


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

        authority_snapshot = _snapshot_execution_authority(authority)
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("cloud execution authority requires a running async task")
        token = self._execution_scope.set(
            _ExecutionScope(authority=authority_snapshot, task=task)
        )
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
            raw_binding = self._binding_resolver(authority)
        except Exception:  # noqa: BLE001 - resolver is a host integration boundary
            raise PermissionError("cloud execution authority could not be resolved") from None
        try:
            binding = _snapshot_standing_binding(raw_binding)
        except Exception:  # noqa: BLE001 - returned binding is an authority boundary
            raise PermissionError("cloud execution authority has no valid standing binding") from None
        if not self._binding_matches_execution(binding, authority):
            raise PermissionError("standing permission does not belong to this execution")
        if provider.kind is not ProviderKind.CLOUD:
            raise PermissionError(
                "standing cloud authority cannot authorize a non-cloud provider"
            )
        effect_network_host = provider.effect_network_host
        if type(effect_network_host) is not str or not effect_network_host:
            raise PermissionError("cloud provider has no trusted effect host")
        if binding.network_host is None or effect_network_host != binding.network_host:
            raise PermissionError("cloud effect host is outside standing permission scope")
        if provider.provider_id != binding.target or request.provider_id != binding.target:
            raise PermissionError("cloud provider is outside standing permission scope")
        if type(request.model) is not str or request.model != binding.resource_id:
            raise PermissionError("cloud model is outside standing permission scope")

        intent = ActionIntent(
            action_id=request.request_id,
            tool_id=_CLOUD_ACTION_CLASS,
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            target=provider.provider_id,
            network_host=effect_network_host,
            task_id=authority.context.task_id,
            project_id=authority.context.project_id,
            site=effect_network_host,
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
            and binding_context.user_id == context.user_id
            and binding_context.project_id == context.project_id
            and binding_context.task_id == context.task_id
        )


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    return value


def _snapshot_permission_context(value: object) -> PermissionContext:
    if type(value) is not PermissionContext:
        raise TypeError("context must be an exact PermissionContext")
    return PermissionContext(
        user_id=_exact_text(value.user_id, "context.user_id"),
        project_id=_exact_text(value.project_id, "context.project_id"),
        task_id=_exact_text(value.task_id, "context.task_id"),
    )


def _snapshot_execution_authority(
    value: object,
) -> StandingPermissionExecutionAuthority:
    if type(value) is not StandingPermissionExecutionAuthority:
        raise TypeError("authority must be exact StandingPermissionExecutionAuthority")
    return StandingPermissionExecutionAuthority(
        subject_id=_exact_text(value.subject_id, "subject_id"),
        context=_snapshot_permission_context(value.context),
    )


def _snapshot_standing_binding(value: object) -> StandingPermissionBinding:
    if type(value) is not StandingPermissionBinding:
        raise TypeError("binding must be an exact StandingPermissionBinding")
    network_host = value.network_host
    if network_host is not None:
        network_host = _exact_text(network_host, "binding.network_host")
    return StandingPermissionBinding(
        permission_id=_exact_text(value.permission_id, "binding.permission_id"),
        subject_id=_exact_text(value.subject_id, "binding.subject_id"),
        context=_snapshot_permission_context(value.context),
        target=_exact_text(value.target, "binding.target"),
        resource_id=_exact_text(value.resource_id, "binding.resource_id"),
        network_host=network_host,
    )
