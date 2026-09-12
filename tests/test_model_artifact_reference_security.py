from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)


def _descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.CLOUD,
        provider_id="provider-public",
        model_id="model-public",
        source_reference="https://models.example.test/catalog/model-public",
        license_reference="https://licenses.example.test/model-public",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
    )


@pytest.mark.parametrize(
    "reference",
    (
        "Bearer MODEL_CANARY",
        "https://models.example.test/%74%6f%6b%65%6e%3dMODEL_CANARY",
        "https://models.example.test/%2574%256f%256b%2565%256e%253dMODEL_CANARY",
        "https://models.example.test/%41uthorization%3A%20Bearer%20MODEL_CANARY",
        "https://models.example.test/%43ookie%3A%20session%3dMODEL_CANARY",
    ),
)
def test_public_reference_rejects_encoded_or_bare_credentials(reference: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        replace(_descriptor(), source_reference=reference)

    assert "MODEL_CANARY" not in str(exc_info.value)


def test_public_reference_preserves_ordinary_public_url() -> None:
    descriptor = _descriptor()

    assert descriptor.source_reference == "https://models.example.test/catalog/model-public"
    assert descriptor.license_reference == "https://licenses.example.test/model-public"
