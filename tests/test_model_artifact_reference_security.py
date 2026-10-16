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
        "x-amz-credential=MODEL_CANARY",
        "x-amz-signature=MODEL_CANARY",
        "x-goog-credential=MODEL_CANARY",
        "x-goog-signature=MODEL_CANARY",
        "id_token=MODEL_CANARY",
        "id-token=MODEL_CANARY",
        "idtoken=MODEL_CANARY",
        "id_token_hint=MODEL_CANARY",
        "id-token-hint=MODEL_CANARY",
        "idtokenhint=MODEL_CANARY",
        "subscription-key=MODEL_CANARY",
        "subscription_key=MODEL_CANARY",
        "subscriptionkey=MODEL_CANARY",
        "AccountKey=MODEL_CANARY",
        "account-key=MODEL_CANARY",
        "account_key=MODEL_CANARY",
        "urn:model:provider-public:accountkey=MODEL_CANARY",
        "urn:model:provider-public:%61ccount-key%3DMODEL_CANARY",
        "Ocp-Apim-Subscription-Key: MODEL_CANARY",
        "SharedAccessSignature=MODEL_CANARY",
        (
            "urn:model:azure:"
            "sv=2022-11-02&sp=r&se=2030-01-01T00:00:00Z&sr=b&sig=MODEL_CANARY"
        ),
        (
            "urn:model:azure:"
            "%73v%3D2022-11-02%26%73p%3Dr%26%73e%3D2030-01-01T00%3A00%3A00Z"
            "%26%73r%3Db%26%73ig%3DMODEL_CANARY"
        ),
        "urn:model:provider-public:id_token=MODEL_CANARY",
        "urn:model:provider-public:id_token_hint=MODEL_CANARY",
        "urn:model:provider-public:subscription-key=MODEL_CANARY",
        "urn:model:provider-public:%73ubscription-key%3DMODEL_CANARY",
        "urn:model:provider-public:%69d_token%3DMODEL_CANARY",
        "urn:model:provider-public:%69d_token_hint%3DMODEL_CANARY",
        "https://models.example.test/%78-amz-credential%3DMODEL_CANARY",
        "https://models.example.test/%78-goog-signature%3DMODEL_CANARY",
    ),
)
def test_public_reference_rejects_encoded_or_bare_credentials(reference: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        replace(_descriptor(), source_reference=reference)

    assert "MODEL_CANARY" not in str(exc_info.value)


@pytest.mark.parametrize(
    "reference",
    (
        "urn:model:provider-public:id_token_count=3",
        "urn:model:provider-public:id_token_hint_count=3",
        "urn:model:provider-public:subscription-key-count=3",
        "urn:model:provider-public:subscription_key_count=3",
        "urn:model:provider-public:subscriptionkey_count=3",
        "urn:model:provider-public:account-key-count=3",
        "urn:model:provider-public:account_key_count=3",
        "urn:model:provider-public:accountkey_count=3",
        "urn:model:provider-public:sig=public-detached-signature",
        "urn:model:provider-public:sv=1&sig=public-signature",
    ),
)
def test_opaque_public_reference_preserves_benign_id_token_metadata(reference: str) -> None:
    descriptor = replace(_descriptor(), source_reference=reference)

    assert descriptor.source_reference == reference


def test_public_reference_preserves_ordinary_public_url() -> None:
    descriptor = _descriptor()

    assert descriptor.source_reference == "https://models.example.test/catalog/model-public"
    assert descriptor.license_reference == "https://licenses.example.test/model-public"
