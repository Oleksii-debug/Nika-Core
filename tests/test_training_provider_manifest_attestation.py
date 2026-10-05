from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.model_gateway.contracts import ModelResponse, ProviderKind
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_model_activation import _activation_attestation_sha256

_REQUEST = "activation-request"
_BINDING = "1" * 64
_ARTIFACT = "2" * 64
_DESCRIPTOR = "3" * 64
_ATTESTOR = "4" * 64
_MANIFEST = "5" * 64


def _attestation(
    *,
    provider_manifest_sha256: str | None = None,
) -> LoadedModelArtifactAttestation:
    return LoadedModelArtifactAttestation(
        request_id=_REQUEST,
        binding_sha256=_BINDING,
        provider_id="ollama",
        model_id="candidate-model",
        artifact_sha256=_ARTIFACT,
        descriptor_digest=_DESCRIPTOR,
        attestor_id="registry-subprocess:test",
        attestor_sha256=_ATTESTOR,
        provider_manifest_sha256=provider_manifest_sha256,
    )


def _result(
    *,
    provider_manifest_sha256: str | None = None,
) -> AttestedModelCompletionResult:
    return AttestedModelCompletionResult(
        response=ModelResponse(
            request_id=_REQUEST,
            text="ok",
            provider_id="ollama",
            provider_kind=ProviderKind.LOCAL,
            model="candidate-model",
        ),
        attestation=_attestation(
            provider_manifest_sha256=provider_manifest_sha256,
        ),
    )


def _legacy_digest() -> str:
    payload = {
        "request_id": _REQUEST,
        "binding_sha256": _BINDING,
        "provider_id": "ollama",
        "model_id": "candidate-model",
        "artifact_sha256": _ARTIFACT,
        "descriptor_digest": _DESCRIPTOR,
        "attestor_id": "registry-subprocess:test",
        "attestor_sha256": _ATTESTOR,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_no_manifest_preserves_legacy_activation_attestation_digest() -> None:
    assert _activation_attestation_sha256(_result()) == _legacy_digest()


def test_provider_manifest_is_bound_into_activation_attestation_digest() -> None:
    legacy = _activation_attestation_sha256(_result())
    with_manifest = _activation_attestation_sha256(
        _result(provider_manifest_sha256=_MANIFEST)
    )

    assert with_manifest != legacy


def test_provider_manifest_survives_exact_attestation_revalidation() -> None:
    original = _attestation(provider_manifest_sha256=_MANIFEST)

    assert original.revalidated() == original
    assert original.revalidated().provider_manifest_sha256 == _MANIFEST


@pytest.mark.parametrize(
    "manifest",
    (
        "5" * 63,
        "F" * 64,
        "sha256:" + "5" * 64,
    ),
)
def test_provider_manifest_rejects_noncanonical_sha256(manifest: str) -> None:
    with pytest.raises(ValueError, match="provider_manifest_sha256"):
        _attestation(provider_manifest_sha256=manifest)
