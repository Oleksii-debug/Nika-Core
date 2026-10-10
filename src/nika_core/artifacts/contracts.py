from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


_MAX_ARTIFACT_METADATA_ITEMS = 256
_MAX_PERCENT_DECODE_PASSES = 8

_FORBIDDEN_SECRET_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "api_token",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "token",
    "x_api_key",
}
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|[/?#&;,:\s])"
    r"(?:access[_-]?token|api[_-]?key|api[_-]?token|authorization|"
    r"client[_-]?secret|cookie|password|private[_-]?key|refresh[_-]?token|"
    r"secret(?:[_-]?key)?|token|x[_-]?api[_-]?key)\s*[:=]",
    re.IGNORECASE,
)


def _require_utf8_text(value: str, *, field_name: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _decode_percent_layers(value: str) -> str:
    current = value
    for _ in range(_MAX_PERCENT_DECODE_PASSES):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    if unquote(current) != current:
        raise ValueError("artifact text exceeds the percent-decoding safety bound")
    return current


def _validate_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_secret_key(value: str) -> str:
    decoded = _decode_percent_layers(value).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", decoded).strip("_")


def _contains_credential_assignment(value: str) -> bool:
    decoded = _decode_percent_layers(value)
    return bool(_CREDENTIAL_ASSIGNMENT.search(decoded)) or "bearer " in decoded.lower()


def _reject_secret_locator(value: str) -> str:
    if _contains_credential_assignment(value):
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
    size_bytes: int = Field(strict=True, ge=0)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=200)
    producer_type: str | None = Field(default=None, max_length=120)
    producer_id: str | None = Field(default=None, max_length=300)
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "idempotency_key",
        "workspace_id",
        "kind",
        "display_name",
        "locator",
        "media_type",
        "producer_type",
        "producer_id",
    )
    @classmethod
    def reject_invalid_utf8_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_utf8_text(value, field_name="artifact text")

    @field_validator("idempotency_key", "workspace_id", "kind")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact identifiers must not be blank")
        return value

    @field_validator("idempotency_key", "workspace_id")
    @classmethod
    def reject_ambiguous_identity_delimiters(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("artifact identity fields must not contain NUL")
        return value

    @field_validator("locator")
    @classmethod
    def reject_locator_credentials(cls, value: str) -> str:
        return _reject_secret_locator(value)

    @field_validator("metadata", mode="before")
    @classmethod
    def bound_metadata_items(cls, value: object) -> object:
        if isinstance(value, dict) and len(value) > _MAX_ARTIFACT_METADATA_ITEMS:
            raise ValueError(
                f"artifact metadata must contain at most {_MAX_ARTIFACT_METADATA_ITEMS} entries"
            )
        return value

    @field_validator("metadata")
    @classmethod
    def reject_secret_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            _require_utf8_text(key, field_name="artifact metadata key")
            _require_utf8_text(item, field_name="artifact metadata value")
            normalized = _normalize_secret_key(key)
            if normalized in _FORBIDDEN_SECRET_KEYS:
                raise ValueError(f"artifact metadata key is reserved for secret material: {key}")
            if len(key) > 120:
                raise ValueError("artifact metadata keys must be at most 120 characters")
            if len(item) > 4096:
                raise ValueError("artifact metadata values must be at most 4096 characters")
            if _contains_credential_assignment(item):
                raise ValueError("artifact metadata values must not contain credential material")
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
    expected_size_bytes: int = Field(strict=True, ge=0)
    actual_size_bytes: int | None = Field(default=None, strict=True, ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str = Field(default="", max_length=500)

    @field_validator("detail")
    @classmethod
    def reject_invalid_detail_utf8(cls, value: str) -> str:
        return _require_utf8_text(value, field_name="artifact verification detail")

    @field_validator("checked_at")
    @classmethod
    def normalize_checked_at(cls, value: datetime) -> datetime:
        return _validate_utc(value, "checked_at")

    @model_validator(mode="after")
    def validate_evidence_truth(self) -> ArtifactVerification:
        has_digest = self.actual_sha256 is not None
        has_size = self.actual_size_bytes is not None
        if has_digest != has_size:
            raise ValueError(
                "artifact verification actual digest and size must be provided together"
            )
        has_actual = has_digest and has_size
        if self.state == ArtifactVerificationState.VERIFIED:
            if (
                not has_actual
                or self.actual_sha256 != self.expected_sha256
                or self.actual_size_bytes != self.expected_size_bytes
            ):
                raise ValueError(
                    "verified artifact evidence must match expected digest and size"
                )
        elif self.state in {
            ArtifactVerificationState.MISSING,
            ArtifactVerificationState.UNAVAILABLE,
        }:
            if has_actual:
                raise ValueError(
                    f"{self.state.value} artifact evidence must not contain actual digest or size"
                )
        elif (
            self.state == ArtifactVerificationState.MISMATCH
            and has_actual
            and self.actual_sha256 == self.expected_sha256
            and self.actual_size_bytes == self.expected_size_bytes
        ):
            raise ValueError(
                "mismatch artifact evidence must differ from expected digest or size"
            )
        return self


class ArtifactRegistryError(RuntimeError):
    pass


class ArtifactConflictError(ArtifactRegistryError):
    pass
