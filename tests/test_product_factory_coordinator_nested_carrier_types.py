from typing import Any

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
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class _HostileHex(str):
    def __eq__(self, _other: object) -> bool:
        return True


class _SuccessForgingCodingResult(CodingResult):
    @property
    def succeeded(self) -> bool:
        return True


def _envelope(**overrides: Any) -> WorkerResultEnvelope:
    values: dict[str, Any] = {
        "work_id": "work-1",
        "component_id": "core",
        "repository_id": "repo-1",
        "base_sha": SHA_A,
        "result_sha": SHA_B,
        "diff_digest": DIFF_DIGEST,
        "coding_result": CodingResult(job_id="work-1"),
        "producer_actor_id": "worker:builder",
    }
    values.update(overrides)
    return WorkerResultEnvelope(**values)


def test_worker_result_rejects_hostile_stale_base_sha_subclass() -> None:
    hostile = _HostileHex("f" * 40)
    assert hostile == SHA_A

    with pytest.raises(CoordinatorError, match="base_sha must be a 40-character hexadecimal SHA"):
        _envelope(base_sha=hostile)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "result_sha",
            _HostileHex("b" * 40),
            "result_sha must be a 40-character hexadecimal SHA",
        ),
        (
            "diff_digest",
            _HostileHex("d" * 64),
            "diff_digest must be a 64-character hexadecimal digest",
        ),
    ),
)
def test_worker_result_rejects_string_subclass_result_authority_carriers(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(CoordinatorError, match=message):
        _envelope(**{field: value})


def test_worker_result_rejects_coding_result_subclass_success_override() -> None:
    forged = _SuccessForgingCodingResult(
        job_id="work-1",
        test_evidence=(TestEvidence(("pytest",), 0, "passing-evidence"),),
        failure=WorkerFailure(WorkerFailureKind.INTERNAL_ERROR, "real worker failure"),
    )
    assert forged.failure is not None
    assert forged.succeeded is True

    with pytest.raises(CoordinatorError, match="coding_result must be exact CodingResult"):
        _envelope(coding_result=forged)


def _running_coordinator(*, review_authority=None):
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
    coordinator = ProductFactoryCoordinator(graph, review_authority=review_authority)
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.start("core")
    return coordinator, request


def _request_envelope(request, coding_result: CodingResult) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIFF_DIGEST,
        coding_result=coding_result,
        producer_actor_id="worker:builder",
    )


def test_record_result_rejects_forged_nested_failure_before_state_mutation() -> None:
    coordinator, request = _running_coordinator()
    failure = WorkerFailure(WorkerFailureKind.INTERNAL_ERROR, "worker failed")
    object.__setattr__(failure, "message", "")
    envelope = _request_envelope(
        request,
        CodingResult(job_id=request.work_id, failure=failure),
    )

    with pytest.raises(CoordinatorError, match="invalid failure evidence"):
        coordinator.record_result(envelope)

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.RUNNING
    assert record.result is None
    assert record.blocker is None


def test_record_result_rejects_forged_nested_test_evidence_before_state_mutation() -> None:
    coordinator, request = _running_coordinator()
    evidence = TestEvidence(("pytest",), 0, "tests-ok")
    object.__setattr__(evidence, "output_digest", "")
    envelope = _request_envelope(
        request,
        CodingResult(job_id=request.work_id, test_evidence=(evidence,)),
    )

    with pytest.raises(CoordinatorError, match="invalid test evidence"):
        coordinator.record_result(envelope)

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.RUNNING
    assert record.result is None


def test_record_result_snapshots_worker_evidence_before_storing_state() -> None:
    coordinator, request = _running_coordinator()
    evidence = TestEvidence(("pytest",), 0, "tests-ok")
    result = CodingResult(job_id=request.work_id, test_evidence=(evidence,))
    envelope = _request_envelope(request, result)

    recorded = coordinator.record_result(envelope)

    assert recorded.state is WorkState.REVIEW_REQUIRED
    assert recorded.result is not None
    assert recorded.result is not envelope
    assert recorded.result.coding_result is not result
    assert recorded.result.coding_result.test_evidence[0] is not evidence

    object.__setattr__(evidence, "output_digest", "")
    object.__setattr__(envelope, "diff_digest", "forged")

    persisted = coordinator.snapshot().records[0]
    assert persisted.result is not None
    assert persisted.result.diff_digest == DIFF_DIGEST
    assert persisted.result.coding_result.test_evidence[0].output_digest == "tests-ok"

def test_restore_snapshots_result_evidence_before_accepting_snapshot() -> None:
    source, request = _running_coordinator()
    evidence = TestEvidence(("pytest",), 0, "tests-ok")
    source.record_result(
        _request_envelope(
            request,
            CodingResult(job_id=request.work_id, test_evidence=(evidence,)),
        )
    )
    external_snapshot = source.snapshot()
    external_result = external_snapshot.records[0].result
    assert external_result is not None
    external_evidence = external_result.coding_result.test_evidence[0]

    restored = ProductFactoryCoordinator(source.graph)
    restored.restore(
        external_snapshot,
        trusted_plan_fingerprint=source.trusted_plan_fingerprint,
    )

    object.__setattr__(external_result, "diff_digest", "forged")
    object.__setattr__(external_evidence, "output_digest", "")

    live = restored.snapshot().records[0]
    assert live.state is WorkState.REVIEW_REQUIRED
    assert live.result is not None
    assert live.result is not external_result
    assert live.result.diff_digest == DIFF_DIGEST
    assert live.result.coding_result.test_evidence[0] is not external_evidence
    assert live.result.coding_result.test_evidence[0].output_digest == "tests-ok"

class _AllowReviewAuthority:
    def verify(self, _subject, _evidence_refs):
        return True


def test_review_snapshots_authorized_decision_before_storing_state() -> None:
    authority = _AllowReviewAuthority()
    coordinator, request = _running_coordinator(review_authority=authority)
    coordinator.record_result(
        _request_envelope(
            request,
            CodingResult(
                job_id=request.work_id,
                test_evidence=(TestEvidence(("pytest",), 0, "tests-ok"),),
            ),
        )
    )
    decision = ReviewDecision(
        reviewer_id="reviewer:qa",
        accepted=True,
        reason="verified",
        evidence_refs=("review:evidence",),
    )

    reviewed = coordinator.review("core", decision)

    assert reviewed.state is WorkState.ACCEPTED
    assert reviewed.review is not None
    assert reviewed.review is not decision

    object.__setattr__(decision, "accepted", False)
    object.__setattr__(decision, "reason", "forged after review")

    live = coordinator.snapshot().records[0]
    assert live.state is WorkState.ACCEPTED
    assert live.review is not None
    assert live.review.accepted is True
    assert live.review.reason == "verified"


def test_restore_snapshots_review_decision_before_accepting_snapshot() -> None:
    authority = _AllowReviewAuthority()
    source, request = _running_coordinator(review_authority=authority)
    source.record_result(
        _request_envelope(
            request,
            CodingResult(
                job_id=request.work_id,
                test_evidence=(TestEvidence(("pytest",), 0, "tests-ok"),),
            ),
        )
    )
    source.review(
        "core",
        ReviewDecision(
            reviewer_id="reviewer:qa",
            accepted=True,
            reason="verified",
            evidence_refs=("review:evidence",),
        ),
    )
    external_snapshot = source.snapshot()
    external_review = external_snapshot.records[0].review
    assert external_review is not None

    restored = ProductFactoryCoordinator(source.graph, review_authority=authority)
    restored.restore(
        external_snapshot,
        trusted_plan_fingerprint=source.trusted_plan_fingerprint,
    )

    object.__setattr__(external_review, "accepted", False)
    object.__setattr__(external_review, "evidence_refs", ("forged:evidence",))

    live = restored.snapshot().records[0]
    assert live.state is WorkState.ACCEPTED
    assert live.review is not None
    assert live.review is not external_review
    assert live.review.accepted is True
    assert live.review.evidence_refs == ("review:evidence",)

