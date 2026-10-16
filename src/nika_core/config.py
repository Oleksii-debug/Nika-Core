from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_path
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

APP_CONFIG_SCHEMA_VERSION = 1
_MAX_PRODUCT_FACTORY_LOCAL_STARTUP_CONFIG_BYTES = 64 * 1024


class AppConfig(BaseSettings):
    """Versioned application settings loaded from explicit values or NIKA_* environment variables."""

    schema_version: int = APP_CONFIG_SCHEMA_VERSION
    app_version: str = "0.0.2"
    database_path: Path = Field(
        default_factory=lambda: user_data_path("NikaCore", appauthor=False) / "nika_core.db",
        validation_alias=AliasChoices("NIKA_DB_PATH", "NIKA_DATABASE_PATH"),
    )
    v01_source_root: Path | None = None
    v01_source_a: Path | None = None
    v01_source_b: Path | None = None
    log_level: str = "INFO"
    model_provider: str = "mock"
    product_factory_local_startup_json: str | None = Field(default=None, repr=False)

    model_config = SettingsConfigDict(
        env_prefix="NIKA_",
        case_sensitive=False,
        extra="forbid",
        validate_default=True,
        populate_by_name=True,
    )

    def __init__(self, **values: object) -> None:
        if "schema_version" in values and type(values["schema_version"]) is not int:
            raise ValueError("explicit schema_version must be an exact integer")
        super().__init__(**values)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        if type(value) is int:
            version = value
        elif type(value) is str:
            if value == str(APP_CONFIG_SCHEMA_VERSION):
                version = APP_CONFIG_SCHEMA_VERSION
            elif value.isascii() and value.isdecimal() and not value.startswith("0"):
                raise ValueError(
                    f"unsupported schema_version: expected {APP_CONFIG_SCHEMA_VERSION}"
                )
            else:
                raise ValueError("schema_version must be the canonical supported version")
        else:
            raise ValueError("schema_version must be the canonical supported version")
        if version != APP_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: expected {APP_CONFIG_SCHEMA_VERSION}"
            )
        return version

    @field_validator("database_path")
    @classmethod
    def validate_database_path(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("database_path must be absolute")
        return expanded

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("model_provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("model_provider must not be empty")
        return normalized

    @field_validator("product_factory_local_startup_json", mode="before")
    @classmethod
    def validate_product_factory_local_startup_json(
        cls, value: object
    ) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("product_factory_local_startup_json must be exact text")
        if not value or value != value.strip() or "\x00" in value:
            raise ValueError(
                "product_factory_local_startup_json must be canonical non-empty text"
            )
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "product_factory_local_startup_json must be UTF-8 text"
            ) from exc
        if len(encoded) > _MAX_PRODUCT_FACTORY_LOCAL_STARTUP_CONFIG_BYTES:
            raise ValueError(
                "product_factory_local_startup_json exceeds the size limit"
            )
        return value

    @classmethod
    def from_environment(cls) -> AppConfig:
        # Both names are supported for compatibility, but selecting one silently
        # when they disagree can start Nika against the wrong durable database.
        # Match the case-insensitive BaseSettings environment contract on every OS.
        aliases = {"nika_db_path", "nika_database_path"}
        overrides = [
            Path(value).expanduser()
            for name, value in os.environ.items()
            if name.casefold() in aliases
        ]
        if overrides and any(path != overrides[0] for path in overrides[1:]):
            raise SettingsError("Суперечливі змінні середовища для шляху бази даних Nika")
        config = cls()
        if getattr(sys, "frozen", False) and "database_path" not in config.model_fields_set:
            from nika_core.reliability.legacy_database import (
                default_legacy_locations,
                prepare_default_database,
            )

            prepare_default_database(config.database_path, default_legacy_locations())
        return config
