from __future__ import annotations

import pytest

from nika_core.intelligence.modes import IntelligenceMode, IntelligenceModePolicy
from nika_core.intelligence.provenance import (
    IntelligenceProvenance,
    IntelligenceResultStatus,
    resolve_model_intelligence_mode,
)
from nika_core.model_gateway.contracts import ProviderKind


def _provenance(**overrides: object) -> IntelligenceProvenance:
    fields: dict[str, object] = {
        "intelligence_mode": IntelligenceMode.EXTERNAL_LOCAL,
        "provider_kind": ProviderKind.LOCAL,
        "provider_id": "ollama",
        "model_fingerprint": "sha256:" + "a" * 64,
        "request_correlation_id": "task:thread",
        "status": IntelligenceResultStatus.SUCCEEDED,
    }
    fields.update(overrides)
    return IntelligenceProvenance(**fields)


@pytest.mark.parametrize(
    "invalid",
    [
        "\x00",
        "\x7f",
        "\x85",
        "safe\u200e-id",
        "safe\u202e-id",
        "safe\u2066-id",
        "safe\u2028-id",
        "safe\u2029-id",
        "\ud800",
        "а" * 257,
        "e" * 513,
        "e" * 511 + "😀",
    ],
)
@pytest.mark.parametrize("field", ["provider_id", "request_correlation_id"])
def test_invalid_provenance_identity_is_rejected(field: str, invalid: str) -> None:
    with pytest.raises(ValueError) as error:
        _provenance(**{field: invalid})
    assert invalid not in str(error.value)


@pytest.mark.parametrize(
    "valid",
    [
        "ollama",
        "провайдер-Олексій-😀",
        "a" * 512,
        "а" * 256,
        "é" * 256,
    ],
)
@pytest.mark.parametrize("field", ["provider_id", "request_correlation_id"])
def test_valid_identity_round_trips_unchanged(field: str, valid: str) -> None:
    provenance = _provenance(**{field: valid})
    payload = provenance.to_payload()
    assert payload[field] == valid
    assert getattr(IntelligenceProvenance.from_payload(payload), field) == valid


@pytest.mark.parametrize("field", ["provider_id", "request_correlation_id"])
def test_corrupt_persisted_provenance_does_not_bypass_admission(field: str) -> None:
    payload = _provenance().to_payload()
    payload[field] = "prefix\u202e-reversed"
    with pytest.raises(ValueError):
        IntelligenceProvenance.from_payload(payload)


@pytest.mark.parametrize("invalid", ["x\u200e", "x\u2029", "\ud800", "а" * 257])
def test_route_resolution_rejects_noncanonical_provider_ids(invalid: str) -> None:
    with pytest.raises(ValueError):
        resolve_model_intelligence_mode(
            provider_id=invalid,
            provider_kind=ProviderKind.LOCAL,
        )


def test_wrong_identity_type_is_rejected_before_serialization() -> None:
    with pytest.raises(TypeError, match="provider_id"):
        _provenance(provider_id=123)
    with pytest.raises(TypeError, match="request_correlation_id"):
        _provenance(request_correlation_id=None)


@pytest.mark.parametrize(
    "invalid",
    [
        "name\x7f",
        "name\x85",
        "name\u200e",
        "name\u202e",
        "name\u2066",
        "name\u2028",
        "name\u2029",
        "\ud800",
        "а" * 65,
        "name" * 33,
    ],
)
@pytest.mark.parametrize(
    "field",
    ["embedded_provider_id", "external_local_provider_id", "external_provider_id"],
)
def test_mode_policy_rejects_invalid_provider_id(field: str, invalid: str) -> None:
    with pytest.raises(ValueError) as error:
        IntelligenceModePolicy(**{field: invalid})
    assert invalid not in str(error.value)


@pytest.mark.parametrize("valid", ["а" * 64, "😀" * 32, "x" * 128])
def test_mode_policy_accepts_exact_utf8_limit(valid: str) -> None:
    policy = IntelligenceModePolicy(embedded_provider_id=valid)
    assert policy.embedded_provider_id == valid
