from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from nika_core.intelligence.modes import IntelligenceMode
from nika_core.model_gateway.contracts import ProviderKind

_PROVENANCE_SCHEMA = "nika.intelligence.provenance.v1"
_MODEL_ORIGIN = "model"
_SHA256_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_ID_LENGTH = 512


class IntelligenceResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class IntelligenceProvenance:
    """Content-free provenance for one model-backed intelligence result.

    Deterministic Brain output deliberately cannot use this contract: its origin is
    not a model.  Provider/model identities are correlation evidence only and never
    carry prompts, responses, credentials, headers or provider diagnostics.
    """

    intelligence_mode: IntelligenceMode
    provider_kind: ProviderKind
    provider_id: str
    model_fingerprint: str
    request_correlation_id: str
    status: IntelligenceResultStatus

    def __post_init__(self) -> None:
        if self.intelligence_mode is IntelligenceMode.DETERMINISTIC:
            raise ValueError("deterministic intelligence is not model-generated")
        expected_kind = _expected_provider_kind(self.intelligence_mode)
        if self.provider_kind is not expected_kind:
            raise ValueError("intelligence mode and provider kind do not match")
        _validate_identifier(self.provider_id, field="provider_id")
        _validate_identifier(
            self.request_correlation_id,
            field="request_correlation_id",
        )
        if _SHA256_FINGERPRINT.fullmatch(self.model_fingerprint) is None:
            raise ValueError("model_fingerprint must be a canonical SHA-256 fingerprint")
        if not isinstance(self.status, IntelligenceResultStatus):
            raise TypeError("status must be IntelligenceResultStatus")

    def to_payload(self) -> dict[str, str]:
        return {
            "schema": _PROVENANCE_SCHEMA,
            "origin": _MODEL_ORIGIN,
            "intelligence_mode": self.intelligence_mode.value,
            "provider_kind": self.provider_kind.value,
            "provider_id": self.provider_id,
            "model_fingerprint": self.model_fingerprint,
            "request_correlation_id": self.request_correlation_id,
            "status": self.status.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> IntelligenceProvenance:
        expected = {
            "schema",
            "origin",
            "intelligence_mode",
            "provider_kind",
            "provider_id",
            "model_fingerprint",
            "request_correlation_id",
            "status",
        }
        if set(payload) != expected:
            raise ValueError("intelligence provenance fields do not match the schema")
        if payload["schema"] != _PROVENANCE_SCHEMA or payload["origin"] != _MODEL_ORIGIN:
            raise ValueError("unsupported intelligence provenance schema or origin")
        try:
            mode = IntelligenceMode(_required_text(payload, "intelligence_mode"))
            provider_kind = ProviderKind(_required_text(payload, "provider_kind"))
            status = IntelligenceResultStatus(_required_text(payload, "status"))
        except ValueError as exc:
            raise ValueError("invalid intelligence provenance enum value") from exc
        return cls(
            intelligence_mode=mode,
            provider_kind=provider_kind,
            provider_id=_required_text(payload, "provider_id"),
            model_fingerprint=_required_text(payload, "model_fingerprint"),
            request_correlation_id=_required_text(payload, "request_correlation_id"),
            status=status,
        )


def resolve_model_intelligence_mode(
    *,
    provider_id: str,
    provider_kind: ProviderKind,
    explicit_mode: IntelligenceMode | None = None,
) -> IntelligenceMode:
    """Resolve one model route without ever relabelling cloud as local or vice versa."""

    if not isinstance(provider_kind, ProviderKind):
        raise TypeError("provider_kind must be ProviderKind")
    _validate_identifier(provider_id, field="provider_id")
    if explicit_mode is None:
        if provider_kind is ProviderKind.CLOUD:
            mode = IntelligenceMode.EXTERNAL_API
        elif provider_kind is ProviderKind.LOCAL:
            mode = (
                IntelligenceMode.EMBEDDED_LOCAL
                if provider_id == "foundry-local"
                else IntelligenceMode.EXTERNAL_LOCAL
            )
        else:
            raise ValueError("model runtime cannot use the deterministic provider kind")
    else:
        if not isinstance(explicit_mode, IntelligenceMode):
            raise TypeError("explicit_mode must be IntelligenceMode")
        mode = explicit_mode

    if mode is IntelligenceMode.DETERMINISTIC:
        raise ValueError("deterministic intelligence is not model-generated")
    if _expected_provider_kind(mode) is not provider_kind:
        raise ValueError("intelligence mode and provider kind do not match")
    return mode


def _expected_provider_kind(mode: IntelligenceMode) -> ProviderKind:
    if mode in {IntelligenceMode.EMBEDDED_LOCAL, IntelligenceMode.EXTERNAL_LOCAL}:
        return ProviderKind.LOCAL
    if mode is IntelligenceMode.EXTERNAL_API:
        return ProviderKind.CLOUD
    raise ValueError("deterministic intelligence has no model provider kind")


def _validate_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if not value or value != value.strip() or len(value) > _MAX_ID_LENGTH:
        raise ValueError(f"{field} must be canonical bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be text")
    return value
