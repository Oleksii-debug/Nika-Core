from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from typing import Protocol
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
    standing_permission_scope_fingerprint,
)
from nika_core.tools import ToolRisk
from nika_core.v01_model_settings import ModelSelection, V01ModelSettings

_CLOUD_ACTION_CLASS = "model.cloud.complete"
_CLOUD_SUBJECT_ID = "nika.packaged.model"
_LOCAL_USER_ID = "nika.local.user"
_GRANT_TTL = timedelta(hours=24)
_BINDING_SCHEMA_VERSION = 1
_TASK_SELECTION_FIELD = "v01_model_selection"


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


class _SQLCursor(Protocol):
    def fetchone(self) -> sqlite3.Row | None: ...


class _SQLExecutor(Protocol):
    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> _SQLCursor: ...


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
        previous_id = self._bound_permission_id(current.task_id, strict=True)
        if previous_id is not None:
            raise CloudModelPermissionDenied(
                "Для нового завдання вже існує неочікуваний дозвіл зовнішньої моделі."
            )
        selection = self._cloud_selection(current.task_id)
        if selection is None:
            return
        self._confirm_and_grant(
            current,
            selection,
            expected_previous_id=previous_id,
        )

    def admit_resumed_task(self, record: TaskRecord) -> None:
        """Refresh finite authority before a PAUSED task is submitted for resume."""

        current = self._queue.get(record.task_id)
        if current != record or current.state is not TaskState.PAUSED:
            raise CloudModelPermissionDenied(
                "Неможливо безпечно підтвердити зовнішню модель для зміненого продовження."
            )
        selection = self._cloud_selection(current.task_id)
        if selection is None:
            return
        previous_id = self._bound_permission_id(current.task_id, strict=True)
        now = self._utc_now()
        if self._active_bound_permission(current, selection, now=now) is not None:
            return
        self._confirm_and_grant(
            current,
            selection,
            expected_previous_id=previous_id,
        )

    def admit_recovered_task(self, record: TaskRecord) -> None:
        """Refresh cloud authority before a crash-left RUNNING task auto-resumes."""

        current = self._queue.get(record.task_id)
        if current != record or current.state is not TaskState.RUNNING:
            raise CloudModelPermissionDenied(
                "Неможливо безпечно підтвердити зовнішню модель для аварійного відновлення."
            )
        if _TASK_SELECTION_FIELD not in current.payload:
            return
        selection = self._cloud_selection(current.task_id)
        if selection is None:
            return
        previous_id = self._bound_permission_id(current.task_id, strict=True)
        now = self._utc_now()
        if self._active_bound_permission(current, selection, now=now) is not None:
            return
        self._confirm_and_grant(
            current,
            selection,
            expected_previous_id=previous_id,
        )

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
            selection = self._cloud_selection(task_id)
        except Exception:  # noqa: BLE001 - corrupt or disallowed durable route fails closed
            return None
        if selection is None:
            return None
        try:
            permission = self._active_bound_permission(record, selection, now=self._utc_now())
        except Exception:  # noqa: BLE001 - corrupt binding/clock fails closed
            return None
        if permission is None:
            return None
        return StandingPermissionExecutionAuthority(
            subject_id=_CLOUD_SUBJECT_ID,
            context=self._context(record),
        )

    def revoke_task(self, task_id: str) -> None:
        """Revoke only the grant still bound to this task under one write lock."""

        permission_id = self._bound_permission_id(task_id, strict=True)
        if permission_id is None:
            return
        with self._permissions.revoke_transaction(
            permission_id,
            revoked_at=self._utc_now(),
        ) as (transaction, _revoked):
            current_id = self._bound_permission_id(
                task_id,
                strict=True,
                connection=transaction,
            )
            if current_id != permission_id:
                raise CloudModelPermissionDenied(
                    "Збережений дозвіл зовнішньої моделі змінився під час відкликання."
                )

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
        expected_previous_id: str | None,
    ) -> None:
        request = self._grant_request(record, selection)
        confirmation_request = CloudModelGrantRequest(
            task_id=request.task_id,
            provider_id=request.provider_id,
            model=request.model,
            network_host=request.network_host,
            private_data_allowed=request.private_data_allowed,
        )
        try:
            approved = self._confirm(confirmation_request)
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

        instant = self._utc_now()
        permission_id = self._new_permission_id(record.task_id)
        try:
            with self._permissions.grant_transaction(
                permission_id=permission_id,
                scope=self._scope_for_request(
                    record,
                    request,
                    granted_at=instant,
                    expires_at=instant + _GRANT_TTL,
                ),
            ) as (conn, _granted):
                self._bind_permission(
                    task_id=record.task_id,
                    permission_id=permission_id,
                    updated_at=instant,
                    expected_previous_id=expected_previous_id,
                    expected_record=record,
                    expected_selection=selection,
                    connection=conn,
                )
        except Exception:  # noqa: BLE001 - durable permission boundary fails closed
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
            selection = self._cloud_selection(record.task_id)
            if selection is None:
                return None
            permission = self._active_bound_permission(
                record,
                selection,
                now=self._utc_now(),
            )
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
            self._validate_binding_schema(conn, require_bindings=False)
            row = conn.execute(
                "SELECT MAX(version) AS version, "
                "typeof(MAX(version)) AS version_type "
                "FROM v01_cloud_model_permission_schema"
            ).fetchone()
            raw_version = row["version"]
            if raw_version is None:
                if row["version_type"] != "null":
                    raise RuntimeError(
                        "cloud model permission binding schema version has invalid storage type"
                    )
                version = 0
            else:
                if (
                    row["version_type"] != "integer"
                    or type(raw_version) is not int
                    or raw_version < 1
                ):
                    raise RuntimeError(
                        "cloud model permission binding schema version has invalid storage type"
                    )
                version = raw_version
            if version > _BINDING_SCHEMA_VERSION:
                raise RuntimeError(
                    "cloud model permission binding schema is newer than supported"
                )
            if version == 0:
                preexisting_binding_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    ("v01_cloud_model_permission_bindings",),
                ).fetchone()
                if preexisting_binding_table is not None:
                    raise RuntimeError(
                        "cloud model permission binding table exists without schema version"
                    )
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
            self._validate_binding_schema(conn)

    @staticmethod
    def _validate_binding_schema(
        conn: sqlite3.Connection,
        *,
        require_bindings: bool = True,
    ) -> None:
        expected_tables = {
            "v01_cloud_model_permission_schema": (
                ("version", "INTEGER", 0, 1),
                ("applied_at", "TEXT", 1, 0),
            ),
        }
        if require_bindings:
            expected_tables["v01_cloud_model_permission_bindings"] = (
                ("task_id", "TEXT", 0, 1),
                ("permission_id", "TEXT", 1, 0),
                ("updated_at", "TEXT", 1, 0),
            )
        for table_name, expected_columns in expected_tables.items():
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            actual_columns = tuple(
                (
                    row["name"],
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    int(row["pk"]),
                )
                for row in rows
            )
            if actual_columns != expected_columns:
                raise RuntimeError(
                    f"cloud model permission schema shape is invalid for {table_name}"
                )
        if not require_bindings:
            return

        actual_foreign_keys = {
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
                row["match"],
            )
            for row in conn.execute(
                "PRAGMA foreign_key_list(v01_cloud_model_permission_bindings)"
            ).fetchall()
        }
        expected_foreign_keys = {
            ("tasks", "task_id", "task_id", "NO ACTION", "CASCADE", "NONE"),
            (
                "standing_permissions",
                "permission_id",
                "permission_id",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        }
        if actual_foreign_keys != expected_foreign_keys:
            raise RuntimeError(
                "cloud model permission binding foreign keys are invalid"
            )

        permission_unique = False
        for index in conn.execute(
            "PRAGMA index_list(v01_cloud_model_permission_bindings)"
        ).fetchall():
            if int(index["unique"]) != 1 or int(index["partial"]) != 0:
                continue
            index_name = str(index["name"]).replace("'", "''")
            columns = tuple(
                row["name"]
                for row in conn.execute(
                    f"PRAGMA index_info('{index_name}')"
                ).fetchall()
            )
            if columns == ("permission_id",):
                permission_unique = True
                break
        if not permission_unique:
            raise RuntimeError(
                "cloud model permission binding permission_id must be unique"
            )

        migrations = conn.execute(
            "SELECT version, typeof(version) AS version_type, "
            "applied_at, typeof(applied_at) AS applied_at_type "
            "FROM v01_cloud_model_permission_schema ORDER BY version"
        ).fetchall()
        if (
            len(migrations) != 1
            or migrations[0]["version_type"] != "integer"
            or migrations[0]["version"] != _BINDING_SCHEMA_VERSION
        ):
            raise RuntimeError(
                "cloud model permission binding migration history is invalid"
            )
        migration = migrations[0]
        if (
            migration["applied_at_type"] != "text"
            or not V01CloudModelPermissionService._is_canonical_utc_timestamp(
                migration["applied_at"]
            )
        ):
            raise RuntimeError(
                "cloud model permission binding migration timestamp is invalid"
            )

    @staticmethod
    def _is_canonical_utc_timestamp(value: object) -> bool:
        if type(value) is not str:
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return (
            parsed.tzinfo is not None
            and parsed.utcoffset() is not None
            and parsed.astimezone(UTC).isoformat() == value
        )

    def _binding_id_from_row(
        self,
        task_id: str,
        row: sqlite3.Row,
        *,
        strict: bool,
    ) -> str | None:
        permission_id = row["permission_id"]
        expected_prefix = self._permission_prefix(task_id)
        suffix = (
            permission_id[len(expected_prefix) :]
            if type(permission_id) is str and permission_id.startswith(expected_prefix)
            else ""
        )
        valid = (
            row["task_id_type"] == "text"
            and type(row["task_id"]) is str
            and row["task_id"] == task_id
            and row["permission_id_type"] == "text"
            and type(permission_id) is str
            and permission_id.startswith(expected_prefix)
            and len(suffix) == 32
            and all(char in "0123456789abcdef" for char in suffix)
            and row["updated_at_type"] == "text"
            and self._is_canonical_utc_timestamp(row["updated_at"])
        )
        if not valid:
            if strict:
                raise CloudModelPermissionDenied(
                    "Збережений зв'язок дозволу зовнішньої моделі пошкоджено."
                )
            return None
        return permission_id

    def _bound_permission_id(
        self,
        task_id: str,
        *,
        strict: bool,
        connection: _SQLExecutor | None = None,
    ) -> str | None:
        if connection is None:
            with self._store.connection() as conn:
                return self._bound_permission_id(
                    task_id,
                    strict=strict,
                    connection=conn,
                )
        row = connection.execute(
            "SELECT task_id, typeof(task_id) AS task_id_type, "
            "permission_id, typeof(permission_id) AS permission_id_type, "
            "updated_at, typeof(updated_at) AS updated_at_type "
            "FROM v01_cloud_model_permission_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._binding_id_from_row(task_id, row, strict=strict)

    def _active_bound_permission(
        self,
        record: TaskRecord,
        selection: ModelSelection,
        *,
        now: datetime,
    ):
        permission_id = self._bound_permission_id(record.task_id, strict=False)
        if permission_id is None:
            return None
        try:
            permission = self._permissions.get(permission_id)
        except Exception:  # noqa: BLE001 - corrupt durable authority must fail closed
            raise CloudModelPermissionDenied(
                "Збережений дозвіл зовнішньої моделі пошкоджено."
            ) from None
        if permission is None or permission.revoked_at is not None:
            return None
        if now < permission.granted_at or now >= permission.expires_at:
            return None
        request = self._grant_request(record, selection)
        expected_scope = self._scope_for_request(
            record,
            request,
            granted_at=permission.granted_at,
            expires_at=permission.expires_at,
        )
        if permission.parent_permission_id is not None:
            return None
        if (
            permission.scope_fingerprint
            != standing_permission_scope_fingerprint(expected_scope)
        ):
            return None
        return permission

    def _bind_permission(
        self,
        *,
        task_id: str,
        permission_id: str,
        updated_at: datetime,
        expected_previous_id: str | None,
        expected_record: TaskRecord,
        expected_selection: ModelSelection,
        connection: _SQLExecutor | None = None,
    ) -> None:
        if connection is None:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._bind_permission(
                    task_id=task_id,
                    permission_id=permission_id,
                    updated_at=updated_at,
                    expected_previous_id=expected_previous_id,
                    expected_record=expected_record,
                    expected_selection=expected_selection,
                    connection=conn,
                )
            return

        expected_payload_json = json.dumps(
            expected_record.payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        expected_selection_json = expected_selection.canonical_json()
        expected_selection_id = hashlib.sha256(
            expected_selection_json.encode("utf-8")
        ).hexdigest()
        if expected_record.payload.get(_TASK_SELECTION_FIELD) != expected_selection_id:
            raise RuntimeError("cloud model task selection changed concurrently")

        selected_row = connection.execute(
            "SELECT selection_json, typeof(selection_json) AS selection_json_type "
            "FROM v01_model_selections WHERE selection_id = ?",
            (expected_selection_id,),
        ).fetchone()
        bound_selection_row = connection.execute(
            "SELECT selection_id, typeof(selection_id) AS selection_id_type, "
            "selection_json, typeof(selection_json) AS selection_json_type "
            "FROM v01_task_model_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if (
            selected_row is None
            or selected_row["selection_json_type"] != "text"
            or selected_row["selection_json"] != expected_selection_json
            or bound_selection_row is None
            or bound_selection_row["selection_id_type"] != "text"
            or bound_selection_row["selection_id"] != expected_selection_id
            or bound_selection_row["selection_json_type"] != "text"
            or bound_selection_row["selection_json"] != expected_selection_json
        ):
            raise RuntimeError("cloud model selection changed concurrently")

        task_row = connection.execute(
            "SELECT workspace_id, agent_id, state, payload_json FROM tasks "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if (
            task_row is None
            or task_row["workspace_id"] != expected_record.workspace_id
            or task_row["agent_id"] != expected_record.agent_id
            or task_row["state"] != expected_record.state.value
            or task_row["payload_json"] != expected_payload_json
        ):
            raise RuntimeError("cloud model permission task changed concurrently")
        row = connection.execute(
            "SELECT task_id, typeof(task_id) AS task_id_type, "
            "permission_id, typeof(permission_id) AS permission_id_type, "
            "updated_at, typeof(updated_at) AS updated_at_type "
            "FROM v01_cloud_model_permission_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        current = (
            None
            if row is None
            else self._binding_id_from_row(task_id, row, strict=True)
        )
        if current != expected_previous_id:
            raise RuntimeError("cloud model permission binding changed concurrently")
        connection.execute(
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
    def _scope_for_request(
        record: TaskRecord,
        request: CloudModelGrantRequest,
        *,
        granted_at: datetime,
        expires_at: datetime,
    ) -> StandingPermissionScope:
        return StandingPermissionScope(
            subject_id=_CLOUD_SUBJECT_ID,
            context=V01CloudModelPermissionService._context(record),
            action_class=_CLOUD_ACTION_CLASS,
            targets=(request.provider_id,),
            sites=(request.network_host,),
            resources=(request.model,),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=granted_at,
            expires_at=expires_at,
        )

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
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("cloud permission clock must return exact timezone-aware datetime")
        return value.astimezone(UTC)
