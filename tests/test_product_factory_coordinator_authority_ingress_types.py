from typing import Any

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ReviewDecision,
    WorkerResultEnvelope,
)
from nika_core.toolsmith.contracts import CodingResult

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64


def _worker_result(**overrides: Any) -> WorkerResultEnvelope:
    values: dict[str, Any] = {
        "work_id": "work-1",
        "component_id": "core",
        "repository_id": "repo-1",
        "base_sha": SHA_A,
        "result_sha": SHA_B,
        "diff_digest": DIGEST,
        "coding_result": CodingResult(job_id="work-1"),
        "producer_actor_id": "team-role:builder",
    }
    values.update(overrides)
    return WorkerResultEnvelope(**values)


def _review(**overrides: Any) -> ReviewDecision:
    values: dict[str, Any] = {
        "reviewer_id": "team-role:qa",
        "accepted": True,
        "reason": "exact candidate accepted",
        "evidence_refs": ("review-evidence:trusted:1",),
    }
    values.update(overrides)
    return ReviewDecision(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"reviewer_id": 1}, "reviewer identity text"),
        ({"accepted": 1}, "exact boolean"),
        ({"reason": object()}, "reason text"),
        ({"evidence_refs": ["review-evidence:trusted:1"]}, "evidence reference text"),
        ({"evidence_refs": (1,)}, "evidence reference text"),
    ),
)
def test_review_decision_malformed_authority_ingress_is_bounded(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(CoordinatorError, match=message):
        _review(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"work_id": 1}, "identity must be non-empty text"),
        ({"base_sha": 1}, "base_sha must be a 40-character hexadecimal SHA"),
        ({"coding_result": "not-a-coding-result"}, "coding_result must be CodingResult"),
        ({"producer_actor_id": 1}, "producer actor identity must be non-empty text"),
    ),
)
def test_worker_result_malformed_producer_ingress_is_bounded(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(CoordinatorError, match=message):
        _worker_result(**overrides)
