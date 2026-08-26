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
SAME_ACTOR_ID = "worker-and-reviewer-same-actor"
PRODUCER_ID = "trusted-producer-actor"
UNASSIGNED_REVIEWER_ID = "invented-unassigned-reviewer"
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
        repositories=(
            RepositoryRef("repo-main", "github", "owner/product", "main"),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-main",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )


def _planned_coordinator() -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement the acceptance-scoped core component"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests"}
        ),
    )
    return coordinator


def _producer_identity_binding() -> tuple[str, str] | None:
    envelope_fields = {item.name for item in fields(WorkerResultEnvelope)}
    coding_result_fields = {item.name for item in fields(CodingResult)}
    for name in ACTOR_FIELD_CANDIDATES:
        if name in envelope_fields:
            return "envelope", name
        if name in coding_result_fields:
            return "coding_result", name
    return None


def _candidate_result(
    request,
    *,
    producer_id: str,
    require_producer_identity: bool,
) -> WorkerResultEnvelope:
    binding = _producer_identity_binding()
    if require_producer_identity and binding is None:
        pytest.fail(
            "PF4 cannot prove independent review: successful candidate evidence "
            "carries no producer/worker actor identity that ProductFactoryCoordinator "
            "can compare with ReviewDecision.reviewer_id"
        )

    coding_kwargs: dict[str, object] = {
        "job_id": request.work_id,
        "test_evidence": (
            TestEvidence(
                request.acceptance_commands[0],
                0,
                "qa-passing-evidence",
            ),
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
    if binding is not None:
        location, actor_field = binding
        if location == "coding_result":
            coding_kwargs[actor_field] = producer_id
        else:
            envelope_kwargs[actor_field] = producer_id

    envelope_kwargs["coding_result"] = CodingResult(**coding_kwargs)
    return WorkerResultEnvelope(**envelope_kwargs)


def test_pf4_same_actor_cannot_accept_its_own_candidate() -> None:
    coordinator = _planned_coordinator()
    request = coordinator.start("core")
    coordinator.record_result(
        _candidate_result(
            request,
            producer_id=SAME_ACTOR_ID,
            require_producer_identity=True,
        )
    )

    with pytest.raises(
        CoordinatorError,
        match=r"(?i)(independent|reviewer|worker|producer|self)",
    ):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=SAME_ACTOR_ID,
                accepted=True,
                reason="same actor attempts to approve its own candidate",
                evidence_refs=("qa:eng11:self-review",),
            ),
        )

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None


def test_pf4_untrusted_reviewer_string_cannot_promote_candidate() -> None:
    coordinator = _planned_coordinator()
    request = coordinator.start("core")
    coordinator.record_result(
        _candidate_result(
            request,
            producer_id=PRODUCER_ID,
            require_producer_identity=False,
        )
    )

    with pytest.raises(
        CoordinatorError,
        match=r"(?i)(trusted|independent|reviewer|evidence|authority)",
    ):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=UNASSIGNED_REVIEWER_ID,
                accepted=True,
                reason="invented reviewer attempts candidate promotion",
                evidence_refs=("invented:untrusted-review-evidence",),
            ),
        )

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None
