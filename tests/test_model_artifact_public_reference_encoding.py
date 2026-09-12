from __future__ import annotations

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)


def _descriptor(reference: str) -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.CLOUD,
        provider_id="provider-a",
        model_id="model-a",
        source_reference=reference,
        license_reference="Apache-2.0",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
    )


@pytest.mark.parametrize(
    "reference",
    (
        "https://alice%3Asekret%40models.example.test/path",
        "https://alice%253Asekret%2540models.example.test/path",
        "https://models.example.test/path%3Fview%3Dpublic",
        "https://models.example.test/path%253Fview%253Dpublic",
        "https://models.example.test/path%23section",
        "https://models.example.test/path%2523section",
        "%2Fetc%2Fprivate-model",
        "%252Fetc%252Fprivate-model",
    ),
)
def test_encoded_public_reference_structure_fails_closed(reference: str) -> None:
    with pytest.raises(ValueError):
        _descriptor(reference)


def test_encoded_userinfo_rejection_does_not_echo_canary() -> None:
    canary = "NIKA_PROVENANCE_SECRET_CANARY"
    reference = f"https://alice%3A{canary}%40models.example.test/path"

    with pytest.raises(ValueError) as error:
        _descriptor(reference)

    assert canary not in str(error.value)


def test_ordinary_public_reference_remains_accepted() -> None:
    descriptor = _descriptor("https://models.example.test/catalog/model-a")

    assert descriptor.source_reference == "https://models.example.test/catalog/model-a"
