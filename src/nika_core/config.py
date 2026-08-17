from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Versioned application settings loaded from explicit values or NIKA_* environment variables."""

    schema_version: int = 1
    app_version: str = "0.0.2"
    database_path: Path = Field(
        default=Path("./data/nika_core.db"),
        validation_alias=AliasChoices("NIKA_DB_PATH", "NIKA_DATABASE_PATH"),
    )
    log_level: str = "INFO"
    model_provider: str = "mock"

    model_config = SettingsConfigDict(
        env_prefix="NIKA_",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
        populate_by_name=True,
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be >= 1")
        return value

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

    @classmethod
    def from_environment(cls) -> AppConfig:
        return cls()
