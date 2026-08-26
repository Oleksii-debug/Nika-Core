from __future__ import annotations

from dataclasses import fields

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
ACTOR_ID = "worker-and-reviewer-same-actor"
ACTOR_FIELD_CANDIDATES = (
    "worker_id",
    "worker_actor_id",
    "producer_id",
    "producer_actor_id",
    "author_id",
    "actor_id",
)


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="qa-pf4-reviewer-separation",
        repositories=(RepositoryRef("repo-main", "github", "owner/product", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-main",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )


def _producer_identity_binding() -> tuple[str, str]:
    envelope_fields = {item.name for item in fields(WorkerResultEnvelope)}
    coding_result_fields = {item.name for item in fields(CodingResult)}
    for name in ACTOR_FIELD_CANDIDATES:
        if name in envelope_fields:
            return "envelope", name
        if name in coding_result_fields:
            return "coding_result", name
    pytest.fail(
        "PF4 cannot prove independent review: successful candidate evidence carries no "
        "producer/worker actor identity that ProductFactoryCoordinator can compare with "
        "ReviewDecision.reviewer_id"
    )


def _successful_result(request) -> WorkerResultEnvelope:
    location, actor_field = _producer_identity_binding()
    coding_kwargs: dict[str, object] = {
        "job_id": request.work_id,
        "test_evidence": (
            TestEvidence(request.acceptance_commands[0], 0, "qa-passing-evidence"),
        ),
    }
    envelope_kwargs: dict[str, object] = {
        "work_id": request.work_id,
        "component_id": request.component_id,
        "repository_id": request.repository_id,
        "base_sha": request.base_sha,
        "result_sha": SHA_B,
        "diff_digest": DIGEST,
    }
    if location == "coding_result":
        coding_kwargs[actor_field] = ACTOR_ID
    else:
        envelope_kwargs[actor_field] = ACTOR_ID
    envelope_kwargs["coding_result"] = CodingResult(**coding_kwargs)
    return WorkerResultEnvelope(**envelope_kwargs)


def test_pf4_same_actor_cannot_accept_its_own_candidate() -> None:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement the acceptance-scoped core component"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    request = coordinator.start("core")
    coordinator.record_result(_successful_result(request))

    with pytest.raises(
        CoordinatorError,
        match=r"(?i)(independent|reviewer|worker|producer|self)",
    ):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=ACTOR_ID,
                accepted=True,
                reason="same actor attempts to approve its own candidate",
                evidence_refs=("qa:eng11:self-review",),
            ),
        )

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None
