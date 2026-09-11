import pytest

from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityError,
    ProductFactoryReviewSubject,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64


def _subject(**overrides):
    values = {
        "project_id": "project-1",
        "component_id": "core",
        "work_id": "work-1",
        "repository_id": "repo-1",
        "base_sha": SHA_A,
        "result_sha": SHA_B,
        "diff_digest": DIGEST,
        "attempt": 1,
        "producer_actor_id": "worker:builder",
        "reviewer_id": "team-role:qa",
        "accepted": True,
    }
    values.update(overrides)
    return ProductFactoryReviewSubject(**values)


def test_review_subject_rejects_bool_as_attempt() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact positive integer"):
        _subject(attempt=True)


def test_review_subject_rejects_integer_as_accepted() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact boolean"):
        _subject(accepted=1)


def test_review_subject_rejects_non_text_actor_identity_with_domain_error() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="non-empty text"):
        _subject(producer_actor_id=1)
