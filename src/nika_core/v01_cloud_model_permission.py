from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
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
from nika_core.v01_model_settings import ModelSelection, V01ModelSettings

_CLOUD_ACTION_CLASS = "model.cloud.complete"
_CLOUD_SUBJECT_ID = "nika.packaged.model"
_LOCAL_USER_ID = "nika.local.user"
_GRANT_TTL = timedelta(hours=24)


class CloudModelPermissionDenied(ValueError):
    """The user declined or the host could not establish cloud-model authority."""


@dataclass(frozen=True, slots=True)
class CloudModelGrantRequest:
    """Secret-free facts shown by the trusted host before one task receives cloud authority."""

    task_id: str
    provider_id: str
    model: str
    network_host: str
    private_data_allowed: bool


CloudModelPermissionConfirm = Callable[[CloudModelGrantRequest], bool]


class V01CloudModelPermissionService:
    """Compose packaged cloud execution with canonical standing-permission authority.

    The task payload and model settings select a route but never mint permission. A trusted
    host callback must explicitly approve the exact already-created task before it can enter
    READY. The durable permission is revalidated at every ModelGateway cloud effect.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        settings: V01ModelSettings,
        confirm: CloudModelPermissionConfirm,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(confirm):
            raise TypeError("confirm must be callable")
        self._settings = settings
        self._queue = TaskQueue(store)
        self._confirm = confirm
        self._clock = clock or (lambda: datetime.now(UTC))
        self._permissions = StandingPermissionStore(store, audit_log=AuditLog(store))
        self._permissions.initialize()
        self._authorizer = StandingPermissionCloudEffectAuthorizer(
            self._permissions,
            self._binding_for_authority,
            clock=self._clock,
        )

    @property
    def cloud_effect_authorizer(self) -> StandingPermissionCloudEffectAuthorizer:
        return self._authorizer

    def admit_created_task(self, record: TaskRecord) -> None:
        """Grant exact cloud authority, or fail before CREATED becomes READY."""

        current = self._queue.get(record.task_id)
        if current != record or current.state is not TaskState.CREATED:
            raise CloudModelPermissionDenied(
                "Неможливо безпечно підтвердити зовнішню модель для зміненого завдання."
            )
        selection = self._settings.for_task(record.task_id)
        if selection.route_kind != "openai_compatible":
            return

        request = self._grant_request(record, selection)
        try:
            approved = self._confirm(request)
        except Exception:  # noqa: BLE001 - trusted host confirmation boundary
            raise CloudModelPermissionDenied(
                "Не вдалося отримати підтвердження для зовнішньої моделі; завдання не запущено."
            ) from None
        if type(approved) is not bool:
            raise CloudModelPermissionDenied(
                "Підтвердження зовнішньої моделі повернуло некоректний результат; "
                "завдання не запущено."
            )
        if not approved:
            raise CloudModelPermissionDenied(
                "Зовнішній API для цього завдання не дозволено; завдання не запущено."
            )

        now = self._utc_now()
        context = self._context(record)
        self._permissions.grant(
            permission_id=self._permission_id(record.task_id),
            scope=StandingPermissionScope(
                subject_id=_CLOUD_SUBJECT_ID,
                context=context,
                action_class=_CLOUD_ACTION_CLASS,
                targets=(request.provider_id,),
                sites=(request.network_host,),
                resources=(request.model,),
                risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
                granted_at=now,
                expires_at=now + _GRANT_TTL,
            ),
        )

    def execution_authority_for_task(
        self,
        task_id: str,
    ) -> StandingPermissionExecutionAuthority | None:
        """Resolve only RUNNING task authority; terminal or paused tasks cannot spend it."""

        try:
            record = self._queue.get(task_id)
        except KeyError:
            return None
        if record.state is not TaskState.RUNNING:
            return None
        try:
            selection = self._settings.for_task(task_id)
        except Exception:  # noqa: BLE001 - corrupt durable route fails closed
            return None
        if selection.route_kind != "openai_compatible":
            return None
        if self._permissions.get(self._permission_id(task_id)) is None:
            return None
        return StandingPermissionExecutionAuthority(
            subject_id=_CLOUD_SUBJECT_ID,
            context=self._context(record),
        )

    def revoke_task(self, task_id: str) -> None:
        """Revoke an existing task grant without fabricating missing authority."""

        permission_id = self._permission_id(task_id)
        if self._permissions.get(permission_id) is not None:
            self._permissions.revoke(permission_id, revoked_at=self._utc_now())

    def _binding_for_authority(
        self,
        authority: StandingPermissionExecutionAuthority,
    ) -> StandingPermissionBinding | None:
        if type(authority) is not StandingPermissionExecutionAuthority:
            return None
        if authority.subject_id != _CLOUD_SUBJECT_ID:
            return None
        try:
            record = self._queue.get(authority.context.task_id)
        except KeyError:
            return None
        if record.state is not TaskState.RUNNING:
            return None
        expected_context = self._context(record)
        if authority.context != expected_context:
            return None
        permission_id = self._permission_id(record.task_id)
        if self._permissions.get(permission_id) is None:
            return None
        try:
            selection = self._settings.for_task(record.task_id)
            request = self._grant_request(record, selection)
        except Exception:  # noqa: BLE001 - durable authority reconstruction fails closed
            return None
        return StandingPermissionBinding(
            permission_id=permission_id,
            subject_id=_CLOUD_SUBJECT_ID,
            context=expected_context,
            target=request.provider_id,
            resource_id=request.model,
            network_host=request.network_host,
        )

    @staticmethod
    def _context(record: TaskRecord) -> PermissionContext:
        return PermissionContext(
            user_id=_LOCAL_USER_ID,
            project_id=record.workspace_id,
            task_id=record.task_id,
        )

    @staticmethod
    def _permission_id(task_id: str) -> str:
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        return f"model-cloud:{digest}"

    @staticmethod
    def _grant_request(
        record: TaskRecord,
        selection: ModelSelection,
    ) -> CloudModelGrantRequest:
        if selection.route_kind != "openai_compatible":
            raise ValueError("cloud permission requires an API model selection")
        provider_id = selection.provider_id
        model = selection.model
        base_url = selection.base_url
        if provider_id is None or model is None or base_url is None:
            raise ValueError("cloud model selection is incomplete")
        host = urlsplit(base_url).hostname
        if host is None:
            raise ValueError("cloud model route has no host")
        return CloudModelGrantRequest(
            task_id=record.task_id,
            provider_id=provider_id,
            model=model,
            network_host=host.lower().rstrip("."),
            private_data_allowed=selection.private_data_allowed,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("cloud permission clock must return timezone-aware datetime")
        return value.astimezone(UTC)
