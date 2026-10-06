from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityError,
    PackagedBuildAuthorityRuntime,
    PackagedBuildAuthorityStore,
    PackagedBuildAuthorityTemplate,
)
from nika_core.product_factory_packaged_build_pass import PackagedReviewedBuildPass
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.ui.bridge_models import UIResult

_SCHEMA = "nika.product-factory.packaged-build-runtime.v1"
_SCHEMA_VERSION = 1
_MAX_CONFIG_BYTES = 128 * 1024
_MAX_TEMPLATES = 128
_MAX_LIST_ITEMS = 64
_MAX_TEXT_BYTES = 4096
_MAX_REVISION = (1 << 63) - 1
_MAX_JSON_INTEGER_DIGITS = 20


class PackagedBuildRuntimeSettingsError(ValueError):
    """User-safe durable packaged PF5 runtime settings failure."""


@dataclass(frozen=True, slots=True)
class PackagedBuildRuntimeConfig:
    node: ExecutionNode
    templates: tuple[PackagedBuildAuthorityTemplate, ...]

    def __post_init__(self) -> None:
        if type(self.node) is not ExecutionNode:
            raise PackagedBuildRuntimeSettingsError(
                "build runtime node must be exact ExecutionNode"
            )
        if (
            type(self.templates) is not tuple
            or not self.templates
            or len(self.templates) > _MAX_TEMPLATES
            or any(type(item) is not PackagedBuildAuthorityTemplate for item in self.templates)
        ):
            raise PackagedBuildRuntimeSettingsError(
                "build runtime requires 1..128 exact component templates"
            )
        keys = tuple(
            (item.project_id, item.repository_id, item.component_id)
            for item in self.templates
        )
        if len(keys) != len(set(keys)):
            raise PackagedBuildRuntimeSettingsError(
                "build runtime component template identities must be unique"
            )
        if any(item.node_id != self.node.identity.node_id for item in self.templates):
            raise PackagedBuildRuntimeSettingsError(
                "build runtime templates must target the configured execution node"
            )
        if any(item.platform is not self.node.identity.platform for item in self.templates):
            raise PackagedBuildRuntimeSettingsError(
                "build runtime template platform must match the configured node"
            )


def activate_packaged_build_runtime(
    store: SQLiteStore,
    *,
    startup: PackagedLocalProductFactoryStartup,
    config: PackagedBuildRuntimeConfig,
) -> PackagedReviewedBuildPass:
    """Activate one launch-frozen PF5 authority set without candidate-derived policy."""

    if type(store) is not SQLiteStore:
        raise TypeError("store must be exact SQLiteStore")
    if type(startup) is not PackagedLocalProductFactoryStartup:
        raise TypeError("startup must be exact PackagedLocalProductFactoryStartup")
    if type(config) is not PackagedBuildRuntimeConfig:
        raise TypeError("config must be exact PackagedBuildRuntimeConfig")

    authorities = PackagedBuildAuthorityStore(
        store,
        node=config.node,
        startup=startup,
    )
    active_keys: set[tuple[str, str, str]] = set()
    for template in config.templates:
        key = (
            template.project_id,
            template.repository_id,
            template.component_id,
        )
        active_keys.add(key)
        try:
            current = authorities.snapshot(
                project_id=template.project_id,
                repository_id=template.repository_id,
                component_id=template.component_id,
            )
        except PackagedBuildAuthorityError as exc:
            if str(exc) != "no packaged build authority is configured for this component":
                raise PackagedBuildRuntimeSettingsError(
                    "Збережену PF5 authority не вдалося безпечно перевірити."
                ) from exc
            try:
                authorities.configure(template, expected_revision=0)
            except PackagedBuildAuthorityError as configure_exc:
                raise PackagedBuildRuntimeSettingsError(
                    "Не вдалося активувати нову PF5 authority."
                ) from configure_exc
        else:
            if current.template != template:
                try:
                    authorities.configure(
                        template,
                        expected_revision=current.revision,
                    )
                except PackagedBuildAuthorityError as exc:
                    raise PackagedBuildRuntimeSettingsError(
                        "PF5 authority змінилася під час startup activation."
                    ) from exc

    runtime = PackagedBuildAuthorityRuntime(authorities)
    return PackagedReviewedBuildPass(
        store=store,
        node=config.node,
        startup=startup,
        authority=runtime,
        configured_components=frozenset(active_keys),
    )


def decode_packaged_build_runtime_config(raw: str) -> PackagedBuildRuntimeConfig:
    if type(raw) is not str or not raw or raw != raw.strip() or "\x00" in raw:
        raise PackagedBuildRuntimeSettingsError(
            "JSON конфігурації PF5 має бути канонічним непорожнім текстом."
        )
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PackagedBuildRuntimeSettingsError(
            "JSON конфігурації PF5 має бути UTF-8."
        ) from exc
    if len(encoded) > _MAX_CONFIG_BYTES:
        raise PackagedBuildRuntimeSettingsError(
            "JSON конфігурації PF5 перевищує допустимий розмір."
        )
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
            parse_int=_bounded_json_int,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise PackagedBuildRuntimeSettingsError(
            "JSON конфігурації PF5 некоректний."
        ) from exc
    if type(payload) is not dict or set(payload) != {"schema", "node", "templates"}:
        raise PackagedBuildRuntimeSettingsError(
            "JSON конфігурації PF5 має неочікувану схему."
        )
    if payload["schema"] != _SCHEMA:
        raise PackagedBuildRuntimeSettingsError(
            "Версія JSON конфігурації PF5 не підтримується."
        )
    node = _decode_node(payload["node"])
    raw_templates = payload["templates"]
    if (
        type(raw_templates) is not list
        or not raw_templates
        or len(raw_templates) > _MAX_TEMPLATES
    ):
        raise PackagedBuildRuntimeSettingsError(
            "PF5 templates мають містити 1..128 елементів."
        )
    try:
        templates = tuple(_decode_template(item) for item in raw_templates)
        return PackagedBuildRuntimeConfig(node=node, templates=templates)
    except (TypeError, ValueError, PackagedBuildAuthorityError) as exc:
        raise PackagedBuildRuntimeSettingsError(
            "PF5 node або component template некоректні."
        ) from exc


class PackagedBuildRuntimeSettings:
    """Persist host-owned local PF5 node/template authority for restart activation."""

    def __init__(self, store: SQLiteStore) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be SQLiteStore")
        self._store = store
        self._audit = AuditLog(store)
        try:
            with store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS product_factory_build_runtime_schema ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                current = (
                    conn.execute(
                        "SELECT MAX(version) FROM product_factory_build_runtime_schema"
                    ).fetchone()[0]
                    or 0
                )
                if type(current) is not int or current < 0:
                    raise PackagedBuildRuntimeSettingsError(
                        "Версія налаштувань PF5 некоректна."
                    )
                if current > _SCHEMA_VERSION:
                    raise PackagedBuildRuntimeSettingsError(
                        "Версія налаштувань PF5 новіша за цю програму."
                    )
                if current < 1:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS product_factory_build_runtime_settings ("
                        "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                        "revision INTEGER NOT NULL CHECK(revision > 0), "
                        "config_json TEXT)"
                    )
                    conn.execute(
                        "INSERT INTO product_factory_build_runtime_schema VALUES (?, ?)",
                        (1, datetime.now(UTC).isoformat()),
                    )
        except sqlite3.Error as exc:
            raise PackagedBuildRuntimeSettingsError(
                "Не вдалося підготувати налаштування PF5."
            ) from exc

    def configure(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            expected_revision, config_json = self._payload(payload)
            if config_json is not None:
                decode_packaged_build_runtime_config(config_json)
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM product_factory_build_runtime_settings "
                    "WHERE singleton = 1"
                ).fetchone()
                revision = self._revision(row)
                if revision != expected_revision:
                    raise PackagedBuildRuntimeSettingsError(
                        "Налаштування PF5 вже змінено в іншому вікні. "
                        "Перечитайте стан перед повтором."
                    )
                if revision >= _MAX_REVISION:
                    raise PackagedBuildRuntimeSettingsError(
                        "Лічильник версії налаштувань PF5 вичерпано."
                    )
                next_revision = revision + 1
                conn.execute(
                    "INSERT INTO product_factory_build_runtime_settings "
                    "(singleton, revision, config_json) VALUES (1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET "
                    "revision=excluded.revision, config_json=excluded.config_json",
                    (next_revision, config_json),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="product_factory.build_runtime.configured",
                    entity_type="product_factory_settings",
                    entity_id="packaged-build-runtime",
                    payload={
                        "revision": next_revision,
                        "configured": config_json is not None,
                    },
                )
            message = (
                "Конфігурацію PF5 збережено. Перезапустіть Nika, щоб застосувати її."
                if config_json is not None
                else (
                    "Конфігурацію PF5 очищено. Перезапустіть Nika, "
                    "щоб вимкнути packaged build runtime."
                )
            )
            return UIResult(
                request_id="product-factory-build-runtime-settings",
                status="completed",
                message=message,
                focus_id="product-factory-build-authority-json",
            )
        except PackagedBuildRuntimeSettingsError as exc:
            message = str(exc)
        except sqlite3.Error:
            message = "Не вдалося зберегти налаштування PF5."
        return UIResult(
            request_id="product-factory-build-runtime-settings",
            status="rejected",
            message=message,
            focus_id="product-factory-build-authority-json",
        )

    def saved_json(self) -> str | None:
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM product_factory_build_runtime_settings "
                    "WHERE singleton = 1"
                ).fetchone()
            if row is None:
                return None
            self._revision(row)
            raw = row["config_json"]
            if raw is None:
                return None
            if type(raw) is not str:
                raise PackagedBuildRuntimeSettingsError(
                    "Збережена конфігурація PF5 пошкоджена."
                )
            decode_packaged_build_runtime_config(raw)
            return raw
        except sqlite3.Error as exc:
            raise PackagedBuildRuntimeSettingsError(
                "Не вдалося прочитати налаштування PF5."
            ) from exc

    def snapshot(self, *, runtime_status: str) -> dict[str, object]:
        if runtime_status not in {
            "active",
            "restart_required",
            "not_configured",
            "invalid",
            "product_factory_required",
        }:
            raise ValueError("PF5 runtime_status is invalid")
        revision = 0
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM product_factory_build_runtime_settings "
                    "WHERE singleton = 1"
                ).fetchone()
            revision = self._revision(row)
            config_json = None
            if row is not None:
                raw = row["config_json"]
                if raw is not None:
                    if type(raw) is not str:
                        raise PackagedBuildRuntimeSettingsError(
                            "Збережена конфігурація PF5 пошкоджена."
                        )
                    decode_packaged_build_runtime_config(raw)
                    config_json = raw
            return {
                "status": "ready",
                "revision": revision,
                "configured": config_json is not None,
                "config_json": config_json,
                "runtime_status": runtime_status,
            }
        except (PackagedBuildRuntimeSettingsError, sqlite3.Error):
            return {
                "status": "invalid",
                "revision": revision,
                "configured": False,
                "config_json": None,
                "runtime_status": "invalid",
            }

    @staticmethod
    def _revision(row: sqlite3.Row | None) -> int:
        if row is None:
            return 0
        revision = row["revision"]
        if type(revision) is not int or not 1 <= revision <= _MAX_REVISION:
            raise PackagedBuildRuntimeSettingsError(
                "Збережена версія налаштувань PF5 некоректна."
            )
        return revision

    @staticmethod
    def _payload(payload: Mapping[str, Any]) -> tuple[int, str | None]:
        if not isinstance(payload, Mapping):
            raise PackagedBuildRuntimeSettingsError(
                "Налаштування PF5 мають бути об’єктом."
            )
        body = dict(payload)
        if set(body) != {"revision", "config_json"}:
            raise PackagedBuildRuntimeSettingsError(
                "Налаштування PF5 мають неочікувані поля."
            )
        revision = body["revision"]
        if type(revision) is not int or not 0 <= revision <= _MAX_REVISION:
            raise PackagedBuildRuntimeSettingsError(
                "Версія налаштувань PF5 некоректна."
            )
        config_json = body["config_json"]
        if config_json is not None and type(config_json) is not str:
            raise PackagedBuildRuntimeSettingsError(
                "JSON конфігурації PF5 має бути текстом."
            )
        return revision, config_json


def _decode_node(value: object) -> ExecutionNode:
    if type(value) is not dict or set(value) != {
        "node_id",
        "platform",
        "architecture",
        "instance_id",
        "features",
        "toolchains",
        "gpu",
        "resources",
    }:
        raise PackagedBuildRuntimeSettingsError("PF5 node має неочікувану схему.")
    try:
        platform = Platform(_text(value, "platform"))
    except ValueError as exc:
        raise PackagedBuildRuntimeSettingsError(
            "PF5 node platform не підтримується."
        ) from exc
    features = frozenset(_text_list(value["features"], "features"))
    toolchains = frozenset(_text_list(value["toolchains"], "toolchains"))
    if "build" not in features:
        raise PackagedBuildRuntimeSettingsError(
            "PF5 node має явно підтримувати feature build."
        )
    gpu = value["gpu"]
    if type(gpu) is not bool:
        raise PackagedBuildRuntimeSettingsError("PF5 node gpu має бути boolean.")
    resources = _resources(value["resources"], "node resources")
    return ExecutionNode(
        NodeIdentity(
            node_id=_text(value, "node_id"),
            platform=platform,
            architecture=_text(value, "architecture"),
            instance_id=_text(value, "instance_id"),
        ),
        NodeCapabilities(features=features, toolchains=toolchains, gpu=gpu),
        resources,
        enabled=True,
    )


def _decode_template(value: object) -> PackagedBuildAuthorityTemplate:
    if type(value) is not dict or set(value) != {
        "project_id",
        "repository_id",
        "component_id",
        "node_id",
        "platform",
        "workspace_relpath",
        "required_features",
        "required_toolchains",
        "resources",
        "command_id",
        "argv",
        "output_paths",
        "max_changed_files",
        "lease_seconds",
        "require_gpu",
    }:
        raise PackagedBuildRuntimeSettingsError(
            "PF5 component template має неочікувану схему."
        )
    try:
        platform = Platform(_text(value, "platform"))
    except ValueError as exc:
        raise PackagedBuildRuntimeSettingsError(
            "PF5 component template platform не підтримується."
        ) from exc
    require_gpu = value["require_gpu"]
    if type(require_gpu) is not bool:
        raise PackagedBuildRuntimeSettingsError(
            "PF5 component template require_gpu має бути boolean."
        )
    return PackagedBuildAuthorityTemplate(
        project_id=_text(value, "project_id"),
        repository_id=_text(value, "repository_id"),
        component_id=_text(value, "component_id"),
        node_id=_text(value, "node_id"),
        platform=platform,
        workspace_relpath=_text(value, "workspace_relpath"),
        required_features=frozenset(
            _text_list(value["required_features"], "required_features")
        ),
        required_toolchains=frozenset(
            _text_list(value["required_toolchains"], "required_toolchains")
        ),
        resources=_resources(value["resources"], "template resources"),
        command_id=_text(value, "command_id"),
        argv=tuple(_text_list(value["argv"], "argv")),
        output_paths=tuple(_text_list(value["output_paths"], "output_paths")),
        max_changed_files=_positive_int(value, "max_changed_files"),
        lease_seconds=_positive_int(value, "lease_seconds"),
        require_gpu=require_gpu,
    )


def _resources(value: object, label: str) -> ResourceEnvelope:
    if type(value) is not dict or set(value) != {"cpu_cores", "memory_mb", "disk_mb"}:
        raise PackagedBuildRuntimeSettingsError(f"{label} має неочікувану схему.")
    return ResourceEnvelope(
        _positive_int(value, "cpu_cores"),
        _positive_int(value, "memory_mb"),
        _positive_int(value, "disk_mb"),
    )


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping[key]
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PackagedBuildRuntimeSettingsError(
            f"PF5 поле {key} має бути канонічним непорожнім текстом."
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PackagedBuildRuntimeSettingsError(
            f"PF5 поле {key} має бути UTF-8."
        ) from exc
    if len(encoded) > _MAX_TEXT_BYTES:
        raise PackagedBuildRuntimeSettingsError(
            f"PF5 поле {key} перевищує допустимий розмір."
        )
    return value


def _text_list(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise PackagedBuildRuntimeSettingsError(
            f"PF5 {label} має бути списком до {_MAX_LIST_ITEMS} елементів."
        )
    result = tuple(_text({"value": item}, "value") for item in value)
    if len(result) != len(set(result)):
        raise PackagedBuildRuntimeSettingsError(
            f"PF5 {label} не може містити дублікати."
        )
    return result


def _positive_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping[key]
    if type(value) is not int or value <= 0:
        raise PackagedBuildRuntimeSettingsError(
            f"PF5 поле {key} має бути exact positive integer."
        )
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(raw: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {raw}")


def _bounded_json_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds digit limit")
    return int(raw)
