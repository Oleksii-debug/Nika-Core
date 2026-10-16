import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
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


class _AllowReviewAuthority:
    def verify(self, subject, evidence_refs):
        return bool(subject.fingerprint and evidence_refs)


class _WorkerResultEnvelopeSubclass(WorkerResultEnvelope):
    pass


class _ReviewDecisionSubclass(ReviewDecision):
    pass


def _coordinator() -> ProductFactoryCoordinator:
    graph = ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("pytest",),),
            ),
        ),
    )
    coordinator = ProductFactoryCoordinator(graph, review_authority=_AllowReviewAuthority())
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    return coordinator


def _result(request, result_type=WorkerResultEnvelope):
    return result_type(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=(TestEvidence(("pytest",), 0, "ok"),),
        ),
        producer_actor_id="builder-1",
    )


def _accepted_coordinator() -> ProductFactoryCoordinator:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_result(request))
    coordinator.review(
        "core",
        ReviewDecision("qa-1", True, "verified", ("review:trusted:1",)),
    )
    return coordinator


def test_record_result_rejects_worker_result_subclass_before_state_mutation() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError, match="exact WorkerResultEnvelope"):
        coordinator.record_result(_result(request, _WorkerResultEnvelopeSubclass))

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.RUNNING
    assert record.result is None


def test_review_rejects_review_decision_subclass_before_state_mutation() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_result(request))

    decision = _ReviewDecisionSubclass(
        "qa-1",
        True,
        "verified",
        ("review:trusted:1",),
    )
    with pytest.raises(CoordinatorError, match="exact ReviewDecision"):
        coordinator.review("core", decision)

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None


def test_restore_rejects_worker_result_subclass() -> None:
    source = _coordinator()
    request = source.start("core")
    source.record_result(_result(request))
    snapshot = source.snapshot()
    record = snapshot.records[0]
    hostile = _result(record.request, _WorkerResultEnvelopeSubclass)
    forged_snapshot = CoordinatorSnapshot(
        project_id=snapshot.project_id,
        revision=snapshot.revision,
        records=(
            WorkRecord(
                request=record.request,
                state=record.state,
                result=hostile,
                review=record.review,
                blocker=record.blocker,
            ),
        ),
        trusted_plan=snapshot.trusted_plan,
    )

    restored = ProductFactoryCoordinator(source.graph, review_authority=_AllowReviewAuthority())
    with pytest.raises(CoordinatorError, match="exact WorkerResultEnvelope"):
        restored.restore(
            forged_snapshot,
            trusted_plan_fingerprint=source.trusted_plan_fingerprint,
        )


def test_restore_rejects_review_decision_subclass() -> None:
    source = _accepted_coordinator()
    snapshot = source.snapshot()
    record = snapshot.records[0]
    hostile_review = _ReviewDecisionSubclass(
        record.review.reviewer_id,
        record.review.accepted,
        record.review.reason,
        record.review.evidence_refs,
    )
    forged_snapshot = CoordinatorSnapshot(
        project_id=snapshot.project_id,
        revision=snapshot.revision,
        records=(
            WorkRecord(
                request=record.request,
                state=record.state,
                result=record.result,
                review=hostile_review,
                blocker=record.blocker,
            ),
        ),
        trusted_plan=snapshot.trusted_plan,
    )

    restored = ProductFactoryCoordinator(source.graph, review_authority=_AllowReviewAuthority())
    with pytest.raises(CoordinatorError, match="exact ReviewDecision"):
        restored.restore(
            forged_snapshot,
            trusted_plan_fingerprint=source.trusted_plan_fingerprint,
        )
