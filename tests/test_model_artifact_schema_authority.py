from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.model_artifact_schema import MODEL_ARTIFACT_SCHEMA_VERSION
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)


def _descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="foundry-local",
        model_id="schema-authority-model",
        source_reference="https://models.example.test/schema-authority-model",
        license_reference="https://licenses.example.test/schema-authority-model",
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
    )


def test_descriptor_uses_canonical_model_artifact_schema_version() -> None:
    descriptor = _descriptor()

    assert descriptor.schema_version == MODEL_ARTIFACT_SCHEMA_VERSION
    assert descriptor.as_dict()["schema_version"] == MODEL_ARTIFACT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "unsupported",
    (MODEL_ARTIFACT_SCHEMA_VERSION - 1, MODEL_ARTIFACT_SCHEMA_VERSION + 1),
)
def test_descriptor_rejects_noncanonical_schema_version(unsupported: int) -> None:
    with pytest.raises(ValueError, match="unsupported model artifact schema_version"):
        replace(_descriptor(), schema_version=unsupported)
