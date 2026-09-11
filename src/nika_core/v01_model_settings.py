from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
    CredentialResolverPort,
    EnvironmentCredentialResolver,
)
from nika_core.model_gateway.contracts import PrivacyClass, ProviderKind
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.ui.bridge_models import UIResult

MAX_MODEL_SETTINGS_REVISION = (1 << 53) - 1
MAX_MODEL_TIMEOUT_SECONDS = 600.0
_SCHEMA_VERSION = 1
_TASK_SELECTION_FIELD = "v01_model_selection"
_SELECTION_ID = re.compile(r"[0-9a-f]{64}")
_ENV_CREDENTIAL_REF = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*")
_MIGRATIONS = {
    1: (
        (
            "CREATE TABLE v01_model_settings ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
            "revision INTEGER NOT NULL CHECK(revision > 0), selection_json TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE v01_model_selections ("
            "selection_id TEXT PRIMARY KEY, selection_json TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE v01_task_model_bindings ("
            "task_id TEXT PRIMARY KEY, selection_id TEXT NOT NULL, "
            "selection_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        ),
    ),
}


class ModelSetupError(ValueError):
    """Fixed user-safe model configuration failure without provider diagnostics."""


class ModelSelection(BaseModel):
    """Secret-free durable identity for one explicit V0.1 model route."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    route_kind: Literal["ollama", "openai_compatible"]
    provider_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=512)
    base_url: str = Field(min_length=1, max_length=2048)
    credential_ref: str | None = Field(default=None, max_length=512, repr=False)
    private_data_allowed: bool = False
    timeout_seconds: float = Field(default=60.0, gt=0.0, le=MAX_MODEL_TIMEOUT_SECONDS)

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_integer(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported model settings schema")
        return value

    @field_validator("provider_id", "model", "base_url")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if value != value.strip() or not value or any(ord(char) < 32 for char in value):
            raise ValueError("invalid model route text")
        return value

    @field_validator("credential_ref")
    @classmethod
    def clean_credential_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid credential reference")
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def finite_timeout(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("timeout_seconds must be finite")
        return number

    @model_validator(mode="after")
    def coherent_route(self) -> ModelSelection:
        if self.route_kind == "ollama":
            if self.provider_id != "ollama":
                raise ValueError("Ollama provider identity must be ollama")
            if self.credential_ref is not None:
                raise ValueError("Ollama route must not contain a credential reference")
            parsed = urlsplit(self.base_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Ollama route requires an HTTP(S) host")
            if parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Ollama local route must use a loopback host")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Ollama route must not contain userinfo")
            if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                raise ValueError("Ollama base URL must not contain path, query, or fragment")
            return self

        if self.provider_id == "ollama":
            raise ValueError("configured API route must not impersonate Ollama")
        if self.credential_ref is None or _ENV_CREDENTIAL_REF.fullmatch(self.credential_ref) is None:
            raise ValueError("configured API route requires an env credential reference")
        ApiModelRouteConfig(
            provider_id=self.provider_id,
            base_url=self.base_url,
            default_model=self.model,
            credential_ref=self.credential_ref,
            supports_private_data=self.private_data_allowed,
        )
        return self

    @property
    def provider_kind(self) -> ProviderKind:
        return ProviderKind.LOCAL if self.route_kind == "ollama" else ProviderKind.CLOUD

    @property
    def effective_private_data_allowed(self) -> bool:
        return True if self.route_kind == "ollama" else self.private_data_allowed

    def canonical_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_stored(cls, value: str) -> ModelSelection:
        try:
            return cls.model_validate_json(value)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ModelSetupError(
                "Збережені налаштування моделі пошкоджені або несумісні."
            ) from exc


class _ModelSetupRequest(ModelSelection):
    revision: int = Field(ge=0, lt=MAX_MODEL_SETTINGS_REVISION)


class V01ModelSettings:
    """Durable user model choice with immutable per-task provider/model binding."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._audit = AuditLog(store)
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS v01_model_settings_schema ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = (
                conn.execute("SELECT MAX(version) FROM v01_model_settings_schema").fetchone()[0]
                or 0
            )
            if type(current) is not int or current < 0:
                raise ModelSetupError("Версія налаштувань моделі некоректна.")
            if current > _SCHEMA_VERSION:
                raise ModelSetupError("Версія налаштувань моделі новіша за цю програму.")
            for version in range(current + 1, _SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO v01_model_settings_schema VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    @staticmethod
    def _revision(row: sqlite3.Row | None) -> int:
        if row is None:
            return 0
        revision = row["revision"]
        if (
            type(revision) is not int
            or revision < 1
            or revision > MAX_MODEL_SETTINGS_REVISION
        ):
            raise ModelSetupError("Збережена версія налаштувань моделі некоректна.")
        return revision

    @staticmethod
    def _selection_id(selection: ModelSelection) -> tuple[str, str]:
        body = selection.canonical_json()
        return hashlib.sha256(body.encode("utf-8")).hexdigest(), body

    @staticmethod
    def _selection_by_id(conn: sqlite3.Connection, selection_id: Any) -> ModelSelection:
        if not isinstance(selection_id, str) or _SELECTION_ID.fullmatch(selection_id) is None:
            raise ModelSetupError("Збережене посилання на модель завдання некоректне.")
        row = conn.execute(
            "SELECT selection_json FROM v01_model_selections WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()
        if row is None or not isinstance(row["selection_json"], str):
            raise ModelSetupError("Збережену модель завдання не знайдено.")
        body = row["selection_json"]
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != selection_id:
            raise ModelSetupError("Збережену модель завдання не вдалося перевірити.")
        return ModelSelection.from_stored(body)

    def _selected(self, conn: sqlite3.Connection) -> ModelSelection:
        row = conn.execute("SELECT * FROM v01_model_settings WHERE singleton = 1").fetchone()
        self._revision(row)
        if row is None:
            raise ModelSetupError("Спочатку виберіть постачальника та модель.")
        return ModelSelection.from_stored(row["selection_json"])

    def configure(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            request = _ModelSetupRequest.model_validate(dict(payload))
            selection = ModelSelection.model_validate(
                request.model_dump(exclude={"revision"})
            )
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM v01_model_settings WHERE singleton = 1"
                ).fetchone()
                if row is not None:
                    ModelSelection.from_stored(row["selection_json"])
                revision = self._revision(row)
                if revision != request.revision:
                    raise ModelSetupError(
                        "Налаштування моделі вже змінено в іншому вікні. "
                        "Перечитайте збережені значення та повторіть збереження."
                    )
                next_revision = revision + 1
                conn.execute(
                    "INSERT INTO v01_model_settings VALUES (1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
                    "selection_json=excluded.selection_json",
                    (next_revision, selection.canonical_json()),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="v01.model.configured",
                    entity_type="model_settings",
                    entity_id="default",
                    payload={
                        "revision": next_revision,
                        "provider_id": selection.provider_id,
                        "provider_kind": selection.provider_kind.value,
                        "model_fingerprint": model_identity_fingerprint(selection.model),
                    },
                )
            return UIResult(
                request_id="model-settings",
                status="completed",
                message="Модель збережено для нових завдань.",
                focus_id="command-input",
            )
        except ModelSetupError as exc:
            message = str(exc)
        except (ValidationError, TypeError, ValueError):
            message = "Перевірте постачальника, модель, адресу, тайм-аут і параметри доступу."
        except sqlite3.Error:
            message = "Не вдалося зберегти налаштування моделі."
        return UIResult(
            request_id="model-settings",
            status="rejected",
            message=message,
            focus_id="model-provider",
        )

    def snapshot(self) -> dict[str, Any]:
        """Return only UI-safe route identity; never expose the credential reference."""

        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM v01_model_settings WHERE singleton = 1"
                ).fetchone()
            if row is None:
                return {"status": "missing", "revision": 0}
            selection = ModelSelection.from_stored(row["selection_json"])
            return {
                "status": "ready",
                "revision": self._revision(row),
                "route_kind": selection.route_kind,
                "provider_id": selection.provider_id,
                "provider_kind": selection.provider_kind.value,
                "model": selection.model,
                "base_url": selection.base_url,
                "timeout_seconds": selection.timeout_seconds,
                "private_data_allowed": selection.effective_private_data_allowed,
                "credential_configured": selection.credential_ref is not None,
            }
        except (sqlite3.Error, ModelSetupError, ValidationError):
            return {"status": "invalid"}

    def prepare_task_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Freeze the current route before TaskQueue accepts the new task."""

        try:
            body = dict(payload)
            if _TASK_SELECTION_FIELD in body:
                raise ModelSetupError("Посилання на модель завдання задає лише Nika.")
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                selection = self._selected(conn)
                selection_id, selection_json = self._selection_id(selection)
                conn.execute(
                    "INSERT OR IGNORE INTO v01_model_selections VALUES (?, ?)",
                    (selection_id, selection_json),
                )
                if self._selection_by_id(conn, selection_id) != selection:
                    raise ModelSetupError("Не вдалося зафіксувати вибір моделі.")
            body[_TASK_SELECTION_FIELD] = selection_id
            return body
        except ModelSetupError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise ModelSetupError(
                "Не вдалося підготувати модель. Завдання не створено."
            ) from exc

    def for_task(self, task_id: str) -> ModelSelection:
        """Return the exact route accepted with the task and bind it once."""

        if not isinstance(task_id, str) or not task_id.strip():
            raise ModelSetupError("Немає коректного ідентифікатора завдання.")
        try:
            payload = TaskQueue(self._store).get(task_id).payload
        except KeyError as exc:
            raise ModelSetupError("Завдання для вибраної моделі не знайдено.") from exc
        selection_id = payload.get(_TASK_SELECTION_FIELD)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            accepted = self._selection_by_id(conn, selection_id)
            row = conn.execute(
                "SELECT selection_id, selection_json FROM v01_task_model_bindings "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is not None:
                if row["selection_id"] != selection_id:
                    raise ModelSetupError(
                        "Модель не збігається з початковою конфігурацією завдання."
                    )
                bound = ModelSelection.from_stored(row["selection_json"])
                if bound != accepted:
                    raise ModelSetupError(
                        "Збережена модель завдання не збігається з прийнятою конфігурацією."
                    )
                return bound
            conn.execute(
                "INSERT INTO v01_task_model_bindings VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    selection_id,
                    accepted.canonical_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._audit.append_with_connection(
                conn,
                event_type="v01.model.bound",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "schema_version": _SCHEMA_VERSION,
                    "provider_id": accepted.provider_id,
                    "provider_kind": accepted.provider_kind.value,
                    "model_fingerprint": model_identity_fingerprint(accepted.model),
                },
            )
            return accepted


class V01BoundModelRuntimeFactory:
    """Reconstruct the existing ModelGatewayAgentRuntime from a task's frozen route."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        definitions: AgentDefinitionRepository,
        settings: V01ModelSettings | None = None,
        credential_resolver: CredentialResolverPort | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._store = store
        self._definitions = definitions
        self._settings = settings or V01ModelSettings(store)
        self._credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self._client_factory = client_factory

    def for_task(self, task_id: str) -> ModelGatewayAgentRuntime:
        selection = self._settings.for_task(task_id)
        return self._runtime_for_selection(selection)

    def supervisor_for_task(
        self,
        task_id: str,
        *,
        store: MultiAgentStore,
    ) -> MultiAgentSupervisor:
        """Build the canonical supervisor with the frozen task route deadline.

        ModelGatewayAgentRuntime accepts a per-request timeout from its caller. The
        canonical MultiAgentSupervisor supplies that request timeout, so the selected
        task deadline must also be bound at this composition boundary rather than
        silently falling back to the supervisor's unrelated default.
        """

        selection = self._settings.for_task(task_id)
        return MultiAgentSupervisor(
            runtime=self._runtime_for_selection(selection),
            store=store,
            definitions=self._definitions,
            runtime_timeout_seconds=selection.timeout_seconds,
        )

    def _runtime_for_selection(self, selection: ModelSelection) -> ModelGatewayAgentRuntime:
        gateway = ModelGateway(audit_log=AuditLog(self._store))
        if selection.route_kind == "ollama":
            gateway.register(
                OllamaProvider(
                    default_model=selection.model,
                    base_url=selection.base_url,
                    think=False,
                    client_factory=self._client_factory,
                ),
                default=True,
            )
        else:
            if selection.credential_ref is None:
                raise ModelSetupError("Збережена API-модель не має посилання на облікові дані.")
            gateway.register(
                CredentialRefOpenAICompatibleProvider(
                    config=ApiModelRouteConfig(
                        provider_id=selection.provider_id,
                        base_url=selection.base_url,
                        default_model=selection.model,
                        credential_ref=selection.credential_ref,
                        supports_private_data=selection.private_data_allowed,
                        supports_hard_cancellation=False,
                    ),
                    credential_resolver=self._credential_resolver,
                    client_factory=self._client_factory,
                ),
                default=True,
            )
        return ModelGatewayAgentRuntime(
            gateway=gateway,
            definitions=self._definitions,
            provider_id=selection.provider_id,
            provider_kind=selection.provider_kind,
            model=selection.model,
            timeout_seconds=selection.timeout_seconds,
            privacy=PrivacyClass.PRIVATE,
            temperature=0.0,
        )
