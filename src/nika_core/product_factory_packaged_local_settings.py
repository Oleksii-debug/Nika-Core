from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartupError,
    decode_packaged_local_product_factory_startup,
)
from nika_core.ui.bridge_models import UIResult

_SCHEMA_VERSION = 1
_MAX_REVISION = (1 << 63) - 1


class PackagedLocalProductFactorySettingsError(ValueError):
    """User-safe durable startup-authority settings failure."""


class PackagedLocalProductFactorySettings:
    """Durable local Product Factory host authority, activated only on restart."""

    def __init__(self, store: SQLiteStore) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be SQLiteStore")
        self._store = store
        self._audit = AuditLog(store)
        try:
            with store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS product_factory_local_startup_schema ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                current = (
                    conn.execute(
                        "SELECT MAX(version) FROM product_factory_local_startup_schema"
                    ).fetchone()[0]
                    or 0
                )
                if type(current) is not int or current < 0:
                    raise PackagedLocalProductFactorySettingsError(
                        "Версія налаштувань Product Factory некоректна."
                    )
                if current > _SCHEMA_VERSION:
                    raise PackagedLocalProductFactorySettingsError(
                        "Версія налаштувань Product Factory новіша за цю програму."
                    )
                if current < 1:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS product_factory_local_startup_settings ("
                        "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                        "revision INTEGER NOT NULL CHECK(revision > 0), "
                        "config_json TEXT NOT NULL)"
                    )
                    conn.execute(
                        "INSERT INTO product_factory_local_startup_schema VALUES (?, ?)",
                        (1, datetime.now(UTC).isoformat()),
                    )
        except sqlite3.Error as exc:
            raise PackagedLocalProductFactorySettingsError(
                "Не вдалося підготувати налаштування Product Factory."
            ) from exc

    @staticmethod
    def _revision(row: sqlite3.Row | None) -> int:
        if row is None:
            return 0
        revision = row["revision"]
        if type(revision) is not int or not 1 <= revision <= _MAX_REVISION:
            raise PackagedLocalProductFactorySettingsError(
                "Збережена версія налаштувань Product Factory некоректна."
            )
        return revision

    @staticmethod
    def _payload(payload: Mapping[str, Any]) -> tuple[int, str | None]:
        if not isinstance(payload, Mapping):
            raise PackagedLocalProductFactorySettingsError(
                "Налаштування Product Factory мають бути об’єктом."
            )
        body = dict(payload)
        if set(body) != {"revision", "config_json"}:
            raise PackagedLocalProductFactorySettingsError(
                "Налаштування Product Factory мають неочікувані поля."
            )
        revision = body["revision"]
        if type(revision) is not int or not 0 <= revision <= _MAX_REVISION:
            raise PackagedLocalProductFactorySettingsError(
                "Версія налаштувань Product Factory некоректна."
            )
        config_json = body["config_json"]
        if config_json is not None and type(config_json) is not str:
            raise PackagedLocalProductFactorySettingsError(
                "JSON конфігурації Product Factory має бути текстом."
            )
        return revision, config_json

    @staticmethod
    def _validate_config(config_json: str) -> str:
        try:
            decoded = decode_packaged_local_product_factory_startup(config_json)
        except PackagedLocalProductFactoryStartupError as exc:
            raise PackagedLocalProductFactorySettingsError(
                "Перевірте JSON конфігурації локального Product Factory."
            ) from exc
        if decoded is None:
            raise PackagedLocalProductFactorySettingsError(
                "JSON конфігурації локального Product Factory порожній."
            )
        return config_json

    def configure(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            expected_revision, config_json = self._payload(payload)
            if config_json is not None:
                config_json = self._validate_config(config_json)
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM product_factory_local_startup_settings "
                    "WHERE singleton = 1"
                ).fetchone()
                revision = self._revision(row)
                if revision != expected_revision:
                    raise PackagedLocalProductFactorySettingsError(
                        "Налаштування Product Factory вже змінено в іншому вікні. "
                        "Перечитайте збережені значення перед повтором."
                    )
                if revision >= _MAX_REVISION:
                    raise PackagedLocalProductFactorySettingsError(
                        "Лічильник версії налаштувань Product Factory вичерпано."
                    )
                next_revision = revision + 1
                if config_json is None:
                    conn.execute(
                        "DELETE FROM product_factory_local_startup_settings "
                        "WHERE singleton = 1"
                    )
                else:
                    conn.execute(
                        "INSERT INTO product_factory_local_startup_settings "
                        "(singleton, revision, config_json) VALUES (1, ?, ?) "
                        "ON CONFLICT(singleton) DO UPDATE SET "
                        "revision=excluded.revision, config_json=excluded.config_json",
                        (next_revision, config_json),
                    )
                self._audit.append_with_connection(
                    conn,
                    event_type="product_factory.local_startup.configured",
                    entity_type="product_factory_settings",
                    entity_id="local-startup",
                    payload={
                        "revision": next_revision,
                        "configured": config_json is not None,
                    },
                )
            message = (
                "Конфігурацію локального Product Factory збережено. "
                "Перезапустіть Nika, щоб застосувати її."
                if config_json is not None
                else (
                    "Збережену конфігурацію локального Product Factory очищено. "
                    "Перезапустіть Nika, щоб застосувати зміну."
                )
            )
            return UIResult(
                request_id="product-factory-local-startup-settings",
                status="completed",
                message=message,
                focus_id="product-factory-local-startup-json",
            )
        except PackagedLocalProductFactorySettingsError as exc:
            message = str(exc)
        except sqlite3.Error:
            message = "Не вдалося зберегти налаштування локального Product Factory."
        return UIResult(
            request_id="product-factory-local-startup-settings",
            status="rejected",
            message=message,
            focus_id="product-factory-local-startup-json",
        )

    def saved_json(self) -> str | None:
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM product_factory_local_startup_settings "
                    "WHERE singleton = 1"
                ).fetchone()
            if row is None:
                return None
            self._revision(row)
            config_json = row["config_json"]
            if type(config_json) is not str:
                raise PackagedLocalProductFactorySettingsError(
                    "Збережена конфігурація Product Factory пошкоджена."
                )
            return self._validate_config(config_json)
        except sqlite3.Error as exc:
            raise PackagedLocalProductFactorySettingsError(
                "Не вдалося прочитати налаштування локального Product Factory."
            ) from exc

    def snapshot(
        self,
        *,
        environment_override: bool,
        runtime_status: str,
    ) -> dict[str, object]:
        if type(environment_override) is not bool:
            raise TypeError("environment_override must be boolean")
        if runtime_status not in {
            "active",
            "restart_required",
            "model_required",
            "not_configured",
            "invalid",
        }:
            raise ValueError("runtime_status is invalid")
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM product_factory_local_startup_settings "
                    "WHERE singleton = 1"
                ).fetchone()
            revision = self._revision(row)
            config_json = None
            if row is not None:
                raw = row["config_json"]
                if type(raw) is not str:
                    raise PackagedLocalProductFactorySettingsError(
                        "Збережена конфігурація Product Factory пошкоджена."
                    )
                config_json = self._validate_config(raw)
            return {
                "status": "ready",
                "revision": revision,
                "configured": config_json is not None,
                "config_json": config_json,
                "environment_override": environment_override,
                "runtime_status": runtime_status,
            }
        except (PackagedLocalProductFactorySettingsError, sqlite3.Error):
            return {
                "status": "invalid",
                "revision": 0,
                "configured": False,
                "config_json": None,
                "environment_override": environment_override,
                "runtime_status": "invalid",
            }
