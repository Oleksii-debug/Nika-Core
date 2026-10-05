from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import (
    IntelligenceMode,
    IntelligenceModeError,
    IntelligenceModePolicy,
    IntelligenceModeRouter,
)
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
    CredentialResolverPort,
    EnvironmentCredentialResolver,
)
from nika_core.model_gateway.contracts import (
    ModelRequest,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.security.model_cloud_authority import (
    StandingPermissionCloudEffectAuthorizer,
    StandingPermissionExecutionAuthority,
)
from nika_core.ui.bridge_models import UIResult
from nika_core.v01_settings_json import bounded_stored_utf8, load_persisted_json_object

MAX_MODEL_SETTINGS_REVISION = (1 << 53) - 1
MAX_MODEL_TIMEOUT_SECONDS = 600.0
_MAX_STORED_SELECTION_BYTES = 64 * 1024
_SCHEMA_VERSION = 2
_TASK_SELECTION_FIELD = "v01_model_selection"
_SELECTION_ID = re.compile(r"[0-9a-f]{64}")
_ENV_CREDENTIAL_REF = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*")
_FORBIDDEN_IDENTITY_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
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
    2: (
        (
            "CREATE TABLE v01_model_promotions ("
            "decision_sha256 TEXT PRIMARY KEY, "
            "binding_sha256 TEXT NOT NULL, "
            "base_artifact_sha256 TEXT NOT NULL, "
            "base_descriptor_digest TEXT NOT NULL, "
            "challenger_artifact_sha256 TEXT NOT NULL, "
            "challenger_descriptor_digest TEXT NOT NULL, "
            "activation_request_sha256 TEXT NOT NULL, "
            "activation_attestation_sha256 TEXT NOT NULL, "
            "previous_selection_id TEXT NOT NULL, "
            "activated_selection_id TEXT NOT NULL, "
            "activated_revision INTEGER NOT NULL CHECK(activated_revision > 0), "
            "rollback_revision INTEGER, "
            "CHECK(rollback_revision IS NULL OR rollback_revision > activated_revision))"
        ),
    ),
}


class ModelSetupError(ValueError):
    """Fixed user-safe model configuration failure without provider diagnostics."""


@dataclass(frozen=True, slots=True)
class ModelPromotionReceipt:
    """Durable, secret-free evidence for one default-model promotion effect."""

    decision_sha256: str
    binding_sha256: str
    base_artifact_sha256: str
    base_descriptor_digest: str
    challenger_artifact_sha256: str
    challenger_descriptor_digest: str
    activation_request_sha256: str
    activation_attestation_sha256: str
    previous_selection_id: str
    activated_selection_id: str
    activated_revision: int
    rollback_revision: int | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.decision_sha256, "decision_sha256"),
            (self.binding_sha256, "binding_sha256"),
            (self.base_artifact_sha256, "base_artifact_sha256"),
            (self.base_descriptor_digest, "base_descriptor_digest"),
            (self.challenger_artifact_sha256, "challenger_artifact_sha256"),
            (self.challenger_descriptor_digest, "challenger_descriptor_digest"),
            (self.activation_request_sha256, "activation_request_sha256"),
            (self.activation_attestation_sha256, "activation_attestation_sha256"),
            (self.previous_selection_id, "previous_selection_id"),
            (self.activated_selection_id, "activated_selection_id"),
        ):
            if type(value) is not str or _SELECTION_ID.fullmatch(value) is None:
                raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
        if (
            type(self.activated_revision) is not int
            or not 1 <= self.activated_revision <= MAX_MODEL_SETTINGS_REVISION
        ):
            raise ValueError("activated_revision is invalid")
        if self.rollback_revision is not None and (
            type(self.rollback_revision) is not int
            or self.rollback_revision <= self.activated_revision
            or self.rollback_revision > MAX_MODEL_SETTINGS_REVISION
        ):
            raise ValueError("rollback_revision is invalid")


class ModelSelection(BaseModel):
    """Secret-free durable identity for one explicit V0.1 intelligence route."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    route_kind: Literal[
        "deterministic",
        "foundry_local",
        "ollama",
        "openai_compatible",
    ]
    provider_id: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=512)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
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
    def clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            value != value.strip()
            or not value
            or any(
                unicodedata.category(char) in _FORBIDDEN_IDENTITY_CATEGORIES
                for char in value
            )
        ):
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
        if self.route_kind == "deterministic":
            if any(
                value is not None
                for value in (
                    self.provider_id,
                    self.model,
                    self.base_url,
                    self.credential_ref,
                )
            ):
                raise ValueError("deterministic route must not name a provider or model")
            return self

        if self.route_kind == "foundry_local":
            if self.provider_id != "foundry-local":
                raise ValueError("embedded provider identity must be foundry-local")
            if self.model is None:
                raise ValueError("embedded route requires an explicit model")
            if self.base_url is not None:
                raise ValueError("embedded route must not contain a network endpoint")
            if self.credential_ref is not None:
                raise ValueError("embedded route must not contain a credential reference")
            return self

        if self.route_kind == "ollama":
            if self.provider_id != "ollama":
                raise ValueError("Ollama provider identity must be ollama")
            if self.model is None or self.base_url is None:
                raise ValueError("Ollama route requires an explicit model and endpoint")
            if self.credential_ref is not None:
                raise ValueError("Ollama route must not contain a credential reference")
            try:
                parsed = urlsplit(self.base_url)
                port = parsed.port
            except ValueError as exc:
                raise ValueError("Ollama route requires a valid explicit port") from exc
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Ollama route requires an HTTP(S) host")
            if parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Ollama local route must use a loopback host")
            if port is None or port == 0:
                raise ValueError("Ollama route requires an explicit non-zero port")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Ollama route must not contain userinfo")
            if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                raise ValueError("Ollama base URL must not contain path, query, or fragment")
            return self

        if self.provider_id is None or self.model is None or self.base_url is None:
            raise ValueError("configured API route requires provider, model, and endpoint")
        if self.provider_id in {"ollama", "foundry-local"}:
            raise ValueError("configured API route must not impersonate a local provider")
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
    def intelligence_mode(self) -> Literal[
        "no_llm",
        "embedded",
        "local_external",
        "api_configured",
    ]:
        return {
            "deterministic": "no_llm",
            "foundry_local": "embedded",
            "ollama": "local_external",
            "openai_compatible": "api_configured",
        }[self.route_kind]

    @property
    def provider_kind(self) -> ProviderKind | None:
        if self.route_kind == "deterministic":
            return None
        if self.route_kind in {"foundry_local", "ollama"}:
            return ProviderKind.LOCAL
        return ProviderKind.CLOUD

    @property
    def effective_private_data_allowed(self) -> bool:
        return True if self.route_kind != "openai_compatible" else self.private_data_allowed

    def canonical_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_stored(cls, value: str) -> ModelSelection:
        try:
            if type(value) is not str:
                raise TypeError("stored model selection must be text")
            decoded = load_persisted_json_object(
                value, max_bytes=_MAX_STORED_SELECTION_BYTES
            )
            return cls.model_validate(decoded)
        except (TypeError, ValueError, ValidationError, RecursionError) as exc:
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
        try:
            raw = bounded_stored_utf8(body, max_bytes=_MAX_STORED_SELECTION_BYTES)
        except (TypeError, ValueError) as exc:
            raise ModelSetupError(
                "Збережену модель завдання не вдалося перевірити."
            ) from exc
        if hashlib.sha256(raw).hexdigest() != selection_id:
            raise ModelSetupError("Збережену модель завдання не вдалося перевірити.")
        return ModelSelection.from_stored(body)

    def _selected(self, conn: sqlite3.Connection) -> ModelSelection:
        row = conn.execute("SELECT * FROM v01_model_settings WHERE singleton = 1").fetchone()
        self._revision(row)
        if row is None:
            raise ModelSetupError("Спочатку виберіть режим та, якщо потрібно, модель.")
        return ModelSelection.from_stored(row["selection_json"])

    @staticmethod
    def _promotion_receipt(row: sqlite3.Row) -> ModelPromotionReceipt:
        try:
            return ModelPromotionReceipt(
                decision_sha256=row["decision_sha256"],
                binding_sha256=row["binding_sha256"],
                base_artifact_sha256=row["base_artifact_sha256"],
                base_descriptor_digest=row["base_descriptor_digest"],
                challenger_artifact_sha256=row["challenger_artifact_sha256"],
                challenger_descriptor_digest=row["challenger_descriptor_digest"],
                activation_request_sha256=row["activation_request_sha256"],
                activation_attestation_sha256=row["activation_attestation_sha256"],
                previous_selection_id=row["previous_selection_id"],
                activated_selection_id=row["activated_selection_id"],
                activated_revision=row["activated_revision"],
                rollback_revision=row["rollback_revision"],
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise ModelSetupError(
                "Збережений запис просування моделі пошкоджений."
            ) from exc

    @staticmethod
    def _require_promotion_digest(value: object, *, field: str) -> str:
        if type(value) is not str or _SELECTION_ID.fullmatch(value) is None:
            raise ModelSetupError(f"{field} має бути точним SHA-256.")
        return value

    def promotion_receipt(
        self,
        decision_sha256: str,
    ) -> ModelPromotionReceipt | None:
        """Read validated durable promotion evidence without changing the route."""

        decision_digest = self._require_promotion_digest(
            decision_sha256,
            field="SHA-256 рішення",
        )
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM v01_model_promotions WHERE decision_sha256 = ?",
                    (decision_digest,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ModelSetupError(
                "Не вдалося надійно прочитати запис просування моделі."
            ) from exc
        if row is None:
            return None
        return self._promotion_receipt(row)

    def activate_promoted_local_model(
        self,
        *,
        expected_revision: int,
        base_provider_id: str,
        base_model_id: str,
        challenger_provider_id: str,
        challenger_model_id: str,
        decision_sha256: str,
        binding_sha256: str,
        base_artifact_sha256: str,
        base_descriptor_digest: str,
        challenger_artifact_sha256: str,
        challenger_descriptor_digest: str,
        activation_request_sha256: str,
        activation_attestation_sha256: str,
    ) -> ModelPromotionReceipt:
        """Atomically activate an attested local challenger for future tasks only.

        The decision digest is the idempotency key. Existing task bindings remain
        immutable because they reference their already-frozen selection IDs.
        """

        decision_digest = self._require_promotion_digest(
            decision_sha256,
            field="SHA-256 рішення",
        )
        binding_digest = self._require_promotion_digest(
            binding_sha256,
            field="SHA-256 зв'язування",
        )
        base_artifact_digest = self._require_promotion_digest(
            base_artifact_sha256,
            field="SHA-256 базового артефакту",
        )
        base_descriptor = self._require_promotion_digest(
            base_descriptor_digest,
            field="SHA-256 базового дескриптора",
        )
        challenger_artifact_digest = self._require_promotion_digest(
            challenger_artifact_sha256,
            field="SHA-256 артефакту-кандидата",
        )
        challenger_descriptor = self._require_promotion_digest(
            challenger_descriptor_digest,
            field="SHA-256 дескриптора-кандидата",
        )
        activation_request_digest = self._require_promotion_digest(
            activation_request_sha256,
            field="SHA-256 запиту активаційної атестації",
        )
        activation_attestation_digest = self._require_promotion_digest(
            activation_attestation_sha256,
            field="SHA-256 активаційної атестації",
        )
        if (
            type(expected_revision) is not int
            or not 1 <= expected_revision < MAX_MODEL_SETTINGS_REVISION
        ):
            raise ModelSetupError("Очікувана версія налаштувань моделі некоректна.")
        for value in (
            base_provider_id,
            base_model_id,
            challenger_provider_id,
            challenger_model_id,
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ModelSetupError("Ідентичність моделі для просування некоректна.")

        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM v01_model_promotions WHERE decision_sha256 = ?",
                    (decision_digest,),
                ).fetchone()
                if existing is not None:
                    receipt = self._promotion_receipt(existing)
                    if receipt.rollback_revision is not None:
                        raise ModelSetupError(
                            "Це просування вже було відкотило і не може бути повторно застосоване."
                        )
                    if (
                        receipt.binding_sha256 != binding_digest
                        or receipt.base_artifact_sha256 != base_artifact_digest
                        or receipt.base_descriptor_digest != base_descriptor
                        or receipt.challenger_artifact_sha256
                        != challenger_artifact_digest
                        or receipt.challenger_descriptor_digest
                        != challenger_descriptor
                        or receipt.activation_request_sha256
                        != activation_request_digest
                        or receipt.activation_attestation_sha256
                        != activation_attestation_digest
                    ):
                        raise ModelSetupError(
                            "Це рішення вже прив'язане до іншого навчального доказу."
                        )
                    previous = self._selection_by_id(
                        conn, receipt.previous_selection_id
                    )
                    activated = self._selection_by_id(
                        conn, receipt.activated_selection_id
                    )
                    if (
                        previous.provider_id != base_provider_id
                        or previous.model != base_model_id
                        or activated.provider_id != challenger_provider_id
                        or activated.model != challenger_model_id
                    ):
                        raise ModelSetupError(
                            "Повтор просування не збігається з початковою моделлю."
                        )
                    return receipt

                row = conn.execute(
                    "SELECT * FROM v01_model_settings WHERE singleton = 1"
                ).fetchone()
                revision = self._revision(row)
                if row is None:
                    raise ModelSetupError(
                        "Спочатку виберіть активну локальну модель."
                    )
                if revision != expected_revision:
                    raise ModelSetupError(
                        "Налаштування моделі вже змінено. Просування не виконано."
                    )
                current = ModelSelection.from_stored(row["selection_json"])
                if (
                    current.route_kind != "ollama"
                    or current.provider_kind is not ProviderKind.LOCAL
                    or current.provider_id != "ollama"
                ):
                    raise ModelSetupError(
                        "Автоматичне просування зараз підтримує лише локальний Ollama."
                    )
                if (
                    current.provider_id != base_provider_id
                    or current.model != base_model_id
                ):
                    raise ModelSetupError(
                        "Поточна модель не збігається з перевіреним чемпіоном."
                    )
                if challenger_provider_id != base_provider_id:
                    raise ModelSetupError(
                        "Зміна локального постачальника потребує окремої конфігурації."
                    )
                if challenger_model_id == base_model_id:
                    raise ModelSetupError(
                        "Модель-кандидат не відрізняється від поточного чемпіона."
                    )
                try:
                    replacement = ModelSelection.model_validate(
                        {
                            **current.model_dump(),
                            "model": challenger_model_id,
                        }
                    )
                except (TypeError, ValueError, ValidationError) as exc:
                    raise ModelSetupError(
                        "Модель-кандидат не утворює коректний локальний маршрут."
                    ) from exc

                previous_id, previous_json = self._selection_id(current)
                activated_id, activated_json = self._selection_id(replacement)
                for selection_id, selection_json, expected in (
                    (previous_id, previous_json, current),
                    (activated_id, activated_json, replacement),
                ):
                    conn.execute(
                        "INSERT OR IGNORE INTO v01_model_selections VALUES (?, ?)",
                        (selection_id, selection_json),
                    )
                    if self._selection_by_id(conn, selection_id) != expected:
                        raise ModelSetupError(
                            "Не вдалося зафіксувати маршрут просування моделі."
                        )

                next_revision = revision + 1
                updated = conn.execute(
                    "UPDATE v01_model_settings SET revision = ?, selection_json = ? "
                    "WHERE singleton = 1 AND revision = ? AND selection_json = ?",
                    (
                        next_revision,
                        activated_json,
                        revision,
                        row["selection_json"],
                    ),
                )
                if updated.rowcount != 1:
                    raise ModelSetupError(
                        "Налаштування моделі змінилися під час просування."
                    )
                conn.execute(
                    "INSERT INTO v01_model_promotions VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        decision_digest,
                        binding_digest,
                        base_artifact_digest,
                        base_descriptor,
                        challenger_artifact_digest,
                        challenger_descriptor,
                        activation_request_digest,
                        activation_attestation_digest,
                        previous_id,
                        activated_id,
                        next_revision,
                    ),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="v01.model.promoted",
                    entity_type="model_settings",
                    entity_id="default",
                    payload={
                        "revision": next_revision,
                        "provider_id": challenger_provider_id,
                        "previous_model_fingerprint": model_identity_fingerprint(
                            base_model_id
                        ),
                        "activated_model_fingerprint": model_identity_fingerprint(
                            challenger_model_id
                        ),
                        "decision_sha256": decision_digest,
                        "binding_sha256": binding_digest,
                        "base_artifact_sha256": base_artifact_digest,
                        "base_descriptor_digest": base_descriptor,
                        "challenger_artifact_sha256": challenger_artifact_digest,
                        "challenger_descriptor_digest": challenger_descriptor,
                        "activation_request_sha256": activation_request_digest,
                        "activation_attestation_sha256": activation_attestation_digest,
                        "rollback_selection_id": previous_id,
                    },
                )
                return ModelPromotionReceipt(
                    decision_sha256=decision_digest,
                    binding_sha256=binding_digest,
                    base_artifact_sha256=base_artifact_digest,
                    base_descriptor_digest=base_descriptor,
                    challenger_artifact_sha256=challenger_artifact_digest,
                    challenger_descriptor_digest=challenger_descriptor,
                    activation_request_sha256=activation_request_digest,
                    activation_attestation_sha256=activation_attestation_digest,
                    previous_selection_id=previous_id,
                    activated_selection_id=activated_id,
                    activated_revision=next_revision,
                )
        except ModelSetupError:
            raise
        except sqlite3.Error as exc:
            raise ModelSetupError(
                "Не вдалося надійно застосувати просування моделі."
            ) from exc

    def rollback_promoted_local_model(
        self,
        *,
        decision_sha256: str,
        binding_sha256: str,
        base_artifact_sha256: str,
        base_descriptor_digest: str,
        challenger_artifact_sha256: str,
        challenger_descriptor_digest: str,
        expected_revision: int,
    ) -> ModelPromotionReceipt:
        """Restore the exact pre-promotion route if the promotion still owns it."""

        decision_digest = self._require_promotion_digest(
            decision_sha256,
            field="SHA-256 рішення",
        )
        binding_digest = self._require_promotion_digest(
            binding_sha256,
            field="SHA-256 зв'язування",
        )
        base_artifact_digest = self._require_promotion_digest(
            base_artifact_sha256,
            field="SHA-256 базового артефакту",
        )
        base_descriptor = self._require_promotion_digest(
            base_descriptor_digest,
            field="SHA-256 базового дескриптора",
        )
        challenger_artifact_digest = self._require_promotion_digest(
            challenger_artifact_sha256,
            field="SHA-256 артефакту-кандидата",
        )
        challenger_descriptor = self._require_promotion_digest(
            challenger_descriptor_digest,
            field="SHA-256 дескриптора-кандидата",
        )
        if (
            type(expected_revision) is not int
            or not 1 <= expected_revision < MAX_MODEL_SETTINGS_REVISION
        ):
            raise ModelSetupError("Очікувана версія налаштувань моделі некоректна.")
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM v01_model_promotions WHERE decision_sha256 = ?",
                    (decision_digest,),
                ).fetchone()
                if row is None:
                    raise ModelSetupError("Запис просування моделі не знайдено.")
                receipt = self._promotion_receipt(row)
                if (
                    receipt.binding_sha256 != binding_digest
                    or receipt.base_artifact_sha256 != base_artifact_digest
                    or receipt.base_descriptor_digest != base_descriptor
                    or receipt.challenger_artifact_sha256
                    != challenger_artifact_digest
                    or receipt.challenger_descriptor_digest
                    != challenger_descriptor
                ):
                    raise ModelSetupError(
                        "Запис відкату належить іншому навчальному доказу."
                    )
                if receipt.rollback_revision is not None:
                    return receipt

                settings_row = conn.execute(
                    "SELECT * FROM v01_model_settings WHERE singleton = 1"
                ).fetchone()
                revision = self._revision(settings_row)
                if settings_row is None:
                    raise ModelSetupError("Поточні налаштування моделі відсутні.")
                if (
                    revision != expected_revision
                    or revision != receipt.activated_revision
                ):
                    raise ModelSetupError(
                        "Після просування модель уже змінено. Відкат зупинено."
                    )
                current = ModelSelection.from_stored(settings_row["selection_json"])
                current_id, _ = self._selection_id(current)
                if current_id != receipt.activated_selection_id:
                    raise ModelSetupError(
                        "Поточний маршрут більше не належить цьому просуванню."
                    )
                previous = self._selection_by_id(
                    conn, receipt.previous_selection_id
                )
                previous_id, previous_json = self._selection_id(previous)
                if previous_id != receipt.previous_selection_id:
                    raise ModelSetupError("Маршрут відкату не пройшов перевірку.")

                next_revision = revision + 1
                updated = conn.execute(
                    "UPDATE v01_model_settings SET revision = ?, selection_json = ? "
                    "WHERE singleton = 1 AND revision = ? AND selection_json = ?",
                    (
                        next_revision,
                        previous_json,
                        revision,
                        settings_row["selection_json"],
                    ),
                )
                if updated.rowcount != 1:
                    raise ModelSetupError(
                        "Налаштування моделі змінилися під час відкату."
                    )
                conn.execute(
                    "UPDATE v01_model_promotions SET rollback_revision = ? "
                    "WHERE decision_sha256 = ? AND rollback_revision IS NULL",
                    (next_revision, decision_digest),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="v01.model.promotion_rolled_back",
                    entity_type="model_settings",
                    entity_id="default",
                    payload={
                        "revision": next_revision,
                        "decision_sha256": decision_digest,
                        "restored_selection_id": receipt.previous_selection_id,
                    },
                )
                return ModelPromotionReceipt(
                    decision_sha256=receipt.decision_sha256,
                    binding_sha256=receipt.binding_sha256,
                    base_artifact_sha256=receipt.base_artifact_sha256,
                    base_descriptor_digest=receipt.base_descriptor_digest,
                    challenger_artifact_sha256=receipt.challenger_artifact_sha256,
                    challenger_descriptor_digest=receipt.challenger_descriptor_digest,
                    previous_selection_id=receipt.previous_selection_id,
                    activated_selection_id=receipt.activated_selection_id,
                    activated_revision=receipt.activated_revision,
                    rollback_revision=next_revision,
                )
        except ModelSetupError:
            raise
        except sqlite3.Error as exc:
            raise ModelSetupError(
                "Не вдалося надійно відкотити просування моделі."
            ) from exc

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
                previous_selection = (
                    ModelSelection.from_stored(row["selection_json"])
                    if row is not None
                    else None
                )
                revision = self._revision(row)
                if revision != request.revision:
                    raise ModelSetupError(
                        "Налаштування моделі вже змінено в іншому вікні. "
                        "Перечитайте збережені значення та повторіть збереження."
                    )
                if previous_selection is not None:
                    previous_id, previous_json = self._selection_id(previous_selection)
                    conn.execute(
                        "INSERT OR IGNORE INTO v01_model_selections VALUES (?, ?)",
                        (previous_id, previous_json),
                    )
                    if self._selection_by_id(conn, previous_id) != previous_selection:
                        raise ModelSetupError("Не вдалося зберегти попередній вибір моделі.")
                next_revision = revision + 1
                conn.execute(
                    "INSERT INTO v01_model_settings VALUES (1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
                    "selection_json=excluded.selection_json",
                    (next_revision, selection.canonical_json()),
                )
                provider_kind = selection.provider_kind
                self._audit.append_with_connection(
                    conn,
                    event_type="v01.model.configured",
                    entity_type="model_settings",
                    entity_id="default",
                    payload={
                        "revision": next_revision,
                        "intelligence_mode": selection.intelligence_mode,
                        "provider_id": selection.provider_id,
                        "provider_kind": (
                            provider_kind.value if provider_kind is not None else None
                        ),
                        "model_fingerprint": (
                            model_identity_fingerprint(selection.model)
                            if selection.model is not None
                            else None
                        ),
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
            message = "Перевірте режим, постачальника, модель, адресу, тайм-аут і параметри доступу."
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
            provider_kind = selection.provider_kind
            return {
                "status": "ready",
                "revision": self._revision(row),
                "intelligence_mode": selection.intelligence_mode,
                "route_kind": selection.route_kind,
                "provider_id": selection.provider_id,
                "provider_kind": provider_kind.value if provider_kind is not None else None,
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
            accepted_provider_kind = accepted.provider_kind
            self._audit.append_with_connection(
                conn,
                event_type="v01.model.bound",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "schema_version": _SCHEMA_VERSION,
                    "intelligence_mode": accepted.intelligence_mode,
                    "provider_id": accepted.provider_id,
                    "provider_kind": (
                        accepted_provider_kind.value
                        if accepted_provider_kind is not None
                        else None
                    ),
                    "model_fingerprint": (
                        model_identity_fingerprint(accepted.model)
                        if accepted.model is not None
                        else None
                    ),
                },
            )
            return accepted


class _TaskBoundCloudEffectAuthorizer:
    """Resolve current outer-task authority on the exact effect task."""

    def __init__(
        self,
        *,
        delegate: StandingPermissionCloudEffectAuthorizer,
        task_id: str,
        authority_resolver: Callable[
            [str],
            StandingPermissionExecutionAuthority | None,
        ],
    ) -> None:
        if type(delegate) is not StandingPermissionCloudEffectAuthorizer:
            raise TypeError("delegate must be StandingPermissionCloudEffectAuthorizer")
        if type(task_id) is not str or not task_id:
            raise TypeError("task_id must be exact non-empty text")
        if not callable(authority_resolver):
            raise TypeError("authority_resolver must be callable")
        self._delegate = delegate
        self._task_id = task_id
        self._authority_resolver = authority_resolver

    def authorize_cloud_effect(
        self,
        *,
        request: ModelRequest,
        provider: ProviderCapabilities,
    ) -> None:
        try:
            authority = self._authority_resolver(self._task_id)
        except Exception:  # noqa: BLE001 - trusted host resolver boundary
            raise PermissionError("cloud execution authority could not be resolved") from None
        if type(authority) is not StandingPermissionExecutionAuthority:
            raise PermissionError("cloud execution has no current host authority")
        if authority.context.task_id != self._task_id:
            raise PermissionError("cloud execution authority belongs to another task")
        with self._delegate.execution_scope(authority):
            self._delegate.authorize_cloud_effect(
                request=request,
                provider=provider,
            )


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
        foundry_manager_factory: Callable[[], Any] | None = None,
        intelligence_policy: IntelligenceModePolicy | None = None,
        cloud_effect_authorizer: StandingPermissionCloudEffectAuthorizer | None = None,
        cloud_execution_authority_resolver: (
            Callable[[str], StandingPermissionExecutionAuthority | None] | None
        ) = None,
    ) -> None:
        if (cloud_effect_authorizer is None) != (
            cloud_execution_authority_resolver is None
        ):
            raise TypeError(
                "cloud effect authorizer and execution authority resolver "
                "must be configured together"
            )
        if (
            cloud_effect_authorizer is not None
            and type(cloud_effect_authorizer) is not StandingPermissionCloudEffectAuthorizer
        ):
            raise TypeError(
                "cloud_effect_authorizer must be StandingPermissionCloudEffectAuthorizer"
            )
        if (
            cloud_execution_authority_resolver is not None
            and not callable(cloud_execution_authority_resolver)
        ):
            raise TypeError("cloud_execution_authority_resolver must be callable")
        self._store = store
        self._definitions = definitions
        self._settings = settings or V01ModelSettings(store)
        self._credential_resolver = credential_resolver or EnvironmentCredentialResolver()
        self._client_factory = client_factory
        self._foundry_manager_factory = foundry_manager_factory
        self._intelligence_policy = intelligence_policy or IntelligenceModePolicy()
        self._cloud_effect_authorizer = cloud_effect_authorizer
        self._cloud_execution_authority_resolver = cloud_execution_authority_resolver

    def for_task(self, task_id: str) -> ModelGatewayAgentRuntime | None:
        selection = self._settings.for_task(task_id)
        if selection.route_kind == "deterministic":
            return None
        return self._runtime_for_selection(selection, task_id=task_id)

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
        if selection.route_kind == "deterministic":
            raise ModelSetupError(
                "Детермінований режим виконується packaged runtime без ModelGateway."
            )
        return MultiAgentSupervisor(
            runtime=self._runtime_for_selection(selection, task_id=task_id),
            store=store,
            definitions=self._definitions,
            runtime_timeout_seconds=selection.timeout_seconds,
        )

    @staticmethod
    def _required_text(value: str | None, *, field: str) -> str:
        if value is None:
            raise ModelSetupError(f"Збережений маршрут моделі не має поля {field}.")
        return value

    def _task_cloud_authorizer(
        self,
        *,
        task_id: str,
    ) -> _TaskBoundCloudEffectAuthorizer | None:
        authorizer = self._cloud_effect_authorizer
        resolver = self._cloud_execution_authority_resolver
        if authorizer is None or resolver is None:
            return None
        return _TaskBoundCloudEffectAuthorizer(
            delegate=authorizer,
            task_id=task_id,
            authority_resolver=resolver,
        )

    def _runtime_for_selection(
        self,
        selection: ModelSelection,
        *,
        task_id: str,
    ) -> ModelGatewayAgentRuntime:
        if selection.route_kind == "deterministic":
            raise ModelSetupError(
                "Детермінований режим не повинен створювати ModelGateway runtime."
            )
        provider_id = self._required_text(selection.provider_id, field="provider_id")
        model = self._required_text(selection.model, field="model")
        provider_kind = selection.provider_kind
        if provider_kind is None:
            raise ModelSetupError("Збережений маршрут моделі не має типу постачальника.")

        cloud_effect_authorizer = (
            self._task_cloud_authorizer(task_id=task_id)
            if provider_kind is ProviderKind.CLOUD
            else None
        )
        gateway = (
            ModelGateway(
                audit_log=AuditLog(self._store),
                cloud_effect_authorizer=cloud_effect_authorizer,
            )
            if cloud_effect_authorizer is not None
            else ModelGateway(audit_log=AuditLog(self._store))
        )
        mode = {
            "foundry_local": IntelligenceMode.EMBEDDED_LOCAL,
            "ollama": IntelligenceMode.EXTERNAL_LOCAL,
            "openai_compatible": IntelligenceMode.EXTERNAL_API,
        }[selection.route_kind]
        try:
            policy_route = IntelligenceModeRouter(
                gateway=gateway,
                policy=self._intelligence_policy,
            ).resolve(mode)
        except IntelligenceModeError:
            raise ModelSetupError(
                "Вибраний режим інтелекту заборонено політикою Nika."
            ) from None
        if policy_route.provider_id != provider_id:
            raise ModelSetupError(
                "Вибраний постачальник не дозволений політикою Nika."
            )

        if selection.route_kind == "foundry_local":
            gateway.register(
                FoundryLocalProvider(
                    default_model=model,
                    manager_factory=self._foundry_manager_factory,
                    allow_download=False,
                ),
                default=True,
            )
        elif selection.route_kind == "ollama":
            base_url = self._required_text(selection.base_url, field="base_url")
            gateway.register(
                OllamaProvider(
                    default_model=model,
                    base_url=base_url,
                    think=False,
                    client_factory=self._client_factory,
                ),
                default=True,
            )
        else:
            base_url = self._required_text(selection.base_url, field="base_url")
            if selection.credential_ref is None:
                raise ModelSetupError("Збережена API-модель не має посилання на облікові дані.")
            gateway.register(
                CredentialRefOpenAICompatibleProvider(
                    config=ApiModelRouteConfig(
                        provider_id=provider_id,
                        base_url=base_url,
                        default_model=model,
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
            provider_id=provider_id,
            provider_kind=provider_kind,
            model=model,
            timeout_seconds=selection.timeout_seconds,
            privacy=PrivacyClass.PRIVATE,
            temperature=0.0,
        )