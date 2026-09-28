from __future__ import annotations

import pytest

from nika_core.learning.study_queue import StudyMaterial, StudyMaterialKind


@pytest.mark.parametrize(
    "source_ref",
    [
        "https://example.test/book.pdf?subscription-key=canary-value-123",
        "https://example.test/book.pdf?subscription_key=canary-value-123",
        "https://example.test/book.pdf?x-api-key=canary-value-123",
        "https://example.test/book.pdf#subscription-key=canary-value-123",
        "https://example.test/book.pdf#subscription_key=canary-value-123",
        "https://example.test/book.pdf#x-api-key=canary-value-123",
        "https://example.test/book.pdf?subscription%2Dkey=canary-value-123",
        "https://example.test/book.pdf?x%2Dapi%2Dkey=canary-value-123",
    ],
)
def test_study_material_rejects_common_credential_query_aliases(source_ref: str) -> None:
    with pytest.raises(ValueError, match="credential"):
        StudyMaterial(
            material_id="credential-alias-regression",
            title="Credential alias regression",
            kind=StudyMaterialKind.BOOK,
            source_ref=source_ref,
            source_version="edition-1",
        )


def test_study_material_keeps_benign_subscription_metadata() -> None:
    material = StudyMaterial(
        material_id="public-subscription-count",
        title="Public metadata",
        kind=StudyMaterialKind.BOOK,
        source_ref="https://example.test/book.pdf?subscription_count=3&chapter=4",
        source_version="edition-1",
    )

    assert material.source_ref.endswith("subscription_count=3&chapter=4")
