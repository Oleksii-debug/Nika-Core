from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

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
_BINDING_SCHEMA_VERSION = 1


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

    Model settings select a route but never mint permission. A trusted host callback must
    approve the exact task before first execution and again after finite authority expires.
    The service persists only the trusted task-to-permission binding; StandingPermissionStore
    remains the sole authority store and revalidates every ModelGateway cloud effect.
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
        self._store = store
        self._settings = settings
        self._queue = TaskQueue(store)
        self._confirm = confirm
        self._clock = clock or (lambda: datetime.now(UTC))
        self._permissions = StandingPermissionStore(store, audit_log=AuditLog(store))
        self._permissions.initialize()
        self._initialize_bindings()
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
        if self._bound_permission_id(record.task_id, strict=True) is not None:
            raise CloudModelPermissionDenied(
                "Для нового завдання вже існує неочікуваний дозвіл зовнішньої моделі."
            )
        selection = self._cloud_selection(record.task_id)
        if selection is None:
            return
        self._confirm_and_grant(record, selection)

    def admit_resumed_task(self, record: TaskRecord) -> None:
        """Refresh finite authority before a PAUSED task is submitted for resume."""

        current = self._queue.get(record.task_id)
        if current != record or current.state is not TaskState.PAUSED:
            raise CloudModelPermissionDenied(
                "Неможливо безпечно підтвердити зовнішню модель для зміненого продовження."
            )
        selection = self._cloud_selection(record.task_id)
        if selection is None:
            return
        now = self._utc_now()
        if self._active_bound_permission(record.task_id, now=now) is not None:
            return
        self._confirm_and_grant(record, selection, now=now)

    def execution_authority_for_task(
        self,
        task_id: str,
    ) -> StandingPermissionExecutionAuthority | None:
        """Resolve only live RUNNING authority; terminal, paused or expired tasks cannot spend it."""

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
        try:
            permission = self._active_bound_permission(task_id, now=self._utc_now())
        except Exception:  # noqa: BLE001 - corrupt binding/clock fails closed
            return None
        if permission is None:
            return None
        return StandingPermissionExecutionAuthority(
            subject_id=_CLOUD_SUBJECT_ID,
            context=self._context(record),
        )

    def revoke_task(self, task_id: str) -> None:
        """Revoke the task's current grant without fabricating missing authority."""

        permission_id = self._bound_permission_id(task_id, strict=True)
        if permission_id is None:
            return
        permission = self._permissions.get(permission_id)
        if permission is not None and permission.revoked_at is None:
            self._permissions.revoke(permission_id, revoked_at=self._utc_now())

    def _cloud_selection(self, task_id: str) -> ModelSelection | None:
        try:
            selection = self._settings.for_task(task_id)
        except Exception:  # noqa: BLE001 - durable model binding fails closed
            raise CloudModelPermissionDenied(
                "Не вдалося безпечно перевірити збережений маршрут моделі для завдання."
            ) from None
        if selection.route_kind != "openai_compatible":
            return None
        if not selection.private_data_allowed:
            raise CloudModelPermissionDenied(
                "Вибраний зовнішній API не має дозволу на приватні дані. "
                "Увімкніть цей параметр у налаштуваннях моделі або виберіть локальний режим."
            )
        return selection

    def _confirm_and_grant(
        self,
        record: TaskRecord,
        selection: ModelSelection,
        *,
        now: datetime | None = None,
    ) -> None:
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

        instant = self._utc_now() if now is None else now
        previous_id = self._bound_permission_id(record.task_id, strict=True)
        permission_id = self._new_permission_id(record.task_id)
        granted = False
        try:
            self._permissions.grant(
                permission_id=permission_id,
                scope=StandingPermissionScope(
                    subject_id=_CLOUD_SUBJECT_ID,
                    context=self._context(record),
                    action_class=_CLOUD_ACTION_CLASS,
                    targets=(request.provider_id,),
                    sites=(request.network_host,),
                    resources=(request.model,),
                    risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
                    granted_at=instant,
                    expires_at=instant + _GRANT_TTL,
                ),
            )
            granted = True
            self._bind_permission(
                task_id=record.task_id,
                permission_id=permission_id,
                updated_at=instant,
                expected_previous_id=previous_id,
            )
        except Exception:  # noqa: BLE001 - durable permission boundary fails closed
            if granted:
                try:
                    self._permissions.revoke(permission_id, revoked_at=instant)
                except Exception:  # noqa: BLE001 - preserve the original admission failure
                    pass
            raise CloudModelPermissionDenied(
                "Не вдалося безпечно зберегти дозвіл для зовнішньої моделі; "
                "завдання не запущено."
            ) from None

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
        try:
            permission = self._active_bound_permission(record.task_id, now=self._utc_now())
            selection = self._settings.for_task(record.task_id)
            request = self._grant_request(record, selection)
        except Exception:  # noqa: BLE001 - durable authority reconstruction fails closed
            return None
        if permission is None:
            return None
        return StandingPermissionBinding(
            permission_id=permission.permission_id,
            subject_id=_CLOUD_SUBJECT_ID,
            context=expected_context,
            target=request.provider_id,
            resource_id=request.model,
            network_host=request.network_host,
        )

    def _initialize_bindings(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS v01_cloud_model_permission_schema ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM v01_cloud_model_permission_schema"
            ).fetchone()
            version = int(row["version"] or 0)
            if version > _BINDING_SCHEMA_VERSION:
                raise RuntimeError(
                    "cloud model permission binding schema is newer than supported"
                )
            if version == 0:
                conn.execute(
                    "CREATE TABLE v01_cloud_model_permission_bindings ("
                    "task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE, "
                    "permission_id TEXT NOT NULL UNIQUE "
                    "REFERENCES standing_permissions(permission_id), "
                    "updated_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO v01_cloud_model_permission_schema(version, applied_at) "
                    "VALUES (?, ?)",
                    (_BINDING_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
                )

    def _bound_permission_id(self, task_id: str, *, strict: bool) -> str | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT permission_id FROM v01_cloud_model_permission_bindings "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        permission_id = row["permission_id"]
        expected_prefix = self._permission_prefix(task_id)
        if not isinstance(permission_id, str) or not permission_id.startswith(expected_prefix):
            if strict:
                raise CloudModelPermissionDenied(
                    "Збережений зв'язок дозволу зовнішньої моделі пошкоджено."
                )
            return None
        return permission_id

    def _active_bound_permission(self, task_id: str, *, now: datetime):
        permission_id = self._bound_permission_id(task_id, strict=False)
        if permission_id is None:
            return None
        permission = self._permissions.get(permission_id)
        if permission is None or permission.revoked_at is not None:
            return None
        if now < permission.granted_at or now >= permission.expires_at:
            return None
        return permission

    def _bind_permission(
        self,
        *,
        task_id: str,
        permission_id: str,
        updated_at: datetime,
        expected_previous_id: str | None,
    ) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT permission_id FROM v01_cloud_model_permission_bindings "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            current = None if row is None else row["permission_id"]
            if current != expected_previous_id:
                raise RuntimeError("cloud model permission binding changed concurrently")
            conn.execute(
                "INSERT INTO v01_cloud_model_permission_bindings"
                "(task_id, permission_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "permission_id=excluded.permission_id, updated_at=excluded.updated_at",
                (task_id, permission_id, updated_at.isoformat()),
            )

    @staticmethod
    def _context(record: TaskRecord) -> PermissionContext:
        return PermissionContext(
            user_id=_LOCAL_USER_ID,
            project_id=record.workspace_id,
            task_id=record.task_id,
        )

    @staticmethod
    def _permission_prefix(task_id: str) -> str:
        task_digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        return f"model-cloud:{task_digest}:"

    @classmethod
    def _new_permission_id(cls, task_id: str) -> str:
        return f"{cls._permission_prefix(task_id)}{uuid4().hex}"

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
