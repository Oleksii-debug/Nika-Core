from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactLocationKind(StrEnum):
    LOCAL_FILE = "local_file"
    OPAQUE_REFERENCE = "opaque_reference"


class ArtifactVerificationState(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


_FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_FORBIDDEN_LOCATOR_MARKERS = (
    "authorization=",
    "authorization:",
    "bearer ",
    "cookie=",
    "password=",
    "token=",
)


def _validate_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _reject_secret_locator(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in _FORBIDDEN_LOCATOR_MARKERS):
        raise ValueError("artifact locator must not contain credential material")
    parsed = urlsplit(value)
    if parsed.scheme and (parsed.username is not None or parsed.password is not None):
        raise ValueError("artifact locator must not contain URL userinfo")
    return value


class ArtifactRecord(FrozenModel):
    artifact_id: str = Field(pattern="^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=300)
    workspace_id: str = Field(min_length=1, max_length=300)
    kind: str = Field(min_length=1, max_length=120)
    display_name: str = Field(default="", max_length=500)
    location_kind: ArtifactLocationKind
    locator: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=200)
    producer_type: str | None = Field(default=None, max_length=120)
    producer_id: str | None = Field(default=None, max_length=300)
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("idempotency_key", "workspace_id", "kind")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact identifiers must not be blank")
        return value

    @field_validator("locator")
    @classmethod
    def reject_locator_credentials(cls, value: str) -> str:
        return _reject_secret_locator(value)

    @field_validator("metadata")
    @classmethod
    def reject_secret_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_SECRET_KEYS:
                raise ValueError(f"artifact metadata key is reserved for secret material: {key}")
            if len(key) > 120:
                raise ValueError("artifact metadata keys must be at most 120 characters")
            if len(item) > 4096:
                raise ValueError("artifact metadata values must be at most 4096 characters")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _validate_utc(value, "created_at")


class ArtifactVerification(FrozenModel):
    verification_id: str = Field(pattern="^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern="^[0-9a-f]{64}$")
    state: ArtifactVerificationState
    expected_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    actual_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    expected_size_bytes: int = Field(ge=0)
    actual_size_bytes: int | None = Field(default=None, ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str = Field(default="", max_length=500)

    @field_validator("checked_at")
    @classmethod
    def normalize_checked_at(cls, value: datetime) -> datetime:
        return _validate_utc(value, "checked_at")


class ArtifactRegistryError(RuntimeError):
    pass


class ArtifactConflictError(ArtifactRegistryError):
    pass
