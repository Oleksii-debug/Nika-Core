from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkState,
    WorkerResultEnvelope,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_review_authority import ProductFactoryReviewSubject
from nika_core.toolsmith.contracts import CodingResult, TestEvidence


SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PRODUCER = "workspace-lease:lease-worker-1"
TRUSTED_REVIEWER = "team-role:qa-reviewer"
UNTRUSTED_REVIEWER = "invented-reviewer"
EVIDENCE = ("review-authority:issued:1",)


@dataclass(slots=True)
class _ExactAuthority:
    trusted_reviewer: str = TRUSTED_REVIEWER
    calls: list[ProductFactoryReviewSubject] = field(default_factory=list)

    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool:
        self.calls.append(subject)
        return subject.reviewer_id == self.trusted_reviewer and evidence_refs == EVIDENCE


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="pf4-review-authority",
        repositories=(RepositoryRef("repo", "github", "owner/product", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )


def _coordinator(authority: _ExactAuthority | None) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(_graph(), review_authority=authority)
    coordinator.plan(
        base_shas={"repo": SHA_A},
        goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    return coordinator


def _record_secure_candidate(
    coordinator: ProductFactoryCoordinator,
    *,
    producer_actor_id: str | None = PRODUCER,
) -> None:
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        command=request.acceptance_commands[0],
                        exit_code=0,
                        output_digest="tests-pass",
                    ),
                ),
            ),
            producer_actor_id=producer_actor_id,
        )
    )


def _record(coordinator: ProductFactoryCoordinator):
    return coordinator.snapshot().records[0]


def test_secure_candidate_cannot_self_review() -> None:
    authority = _ExactAuthority(trusted_reviewer=PRODUCER)
    coordinator = _coordinator(authority)
    _record_secure_candidate(coordinator)

    with pytest.raises(CoordinatorError, match="independent reviewer"):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=PRODUCER,
                accepted=True,
                reason="self review must fail",
                evidence_refs=EVIDENCE,
            ),
        )

    assert _record(coordinator).state is WorkState.REVIEW_REQUIRED
    assert authority.calls == []


def test_secure_candidate_rejects_missing_producer_identity() -> None:
    authority = _ExactAuthority()
    coordinator = _coordinator(authority)
    _record_secure_candidate(coordinator, producer_actor_id=None)

    with pytest.raises(CoordinatorError, match="producer actor identity"):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=TRUSTED_REVIEWER,
                accepted=True,
                reason="anonymous producer must fail closed",
                evidence_refs=EVIDENCE,
            ),
        )

    assert _record(coordinator).state is WorkState.REVIEW_REQUIRED
    assert authority.calls == []


def test_secure_candidate_rejects_untrusted_reviewer_text() -> None:
    authority = _ExactAuthority()
    coordinator = _coordinator(authority)
    _record_secure_candidate(coordinator)

    with pytest.raises(CoordinatorError, match="authority rejected"):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=UNTRUSTED_REVIEWER,
                accepted=True,
                reason="invented reviewer must fail",
                evidence_refs=EVIDENCE,
            ),
        )

    assert _record(coordinator).state is WorkState.REVIEW_REQUIRED
    assert authority.calls[-1].producer_actor_id == PRODUCER
    assert authority.calls[-1].result_sha == SHA_B
    assert authority.calls[-1].diff_digest == DIGEST


def test_secure_candidate_requires_review_authority() -> None:
    coordinator = _coordinator(None)
    _record_secure_candidate(coordinator)

    with pytest.raises(CoordinatorError, match="review authority is required"):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=TRUSTED_REVIEWER,
                accepted=True,
                reason="authority is mandatory",
                evidence_refs=EVIDENCE,
            ),
        )

    assert _record(coordinator).state is WorkState.REVIEW_REQUIRED


def test_exact_authorized_review_survives_restart_revalidation() -> None:
    authority = _ExactAuthority()
    coordinator = _coordinator(authority)
    _record_secure_candidate(coordinator)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id=TRUSTED_REVIEWER,
            accepted=True,
            reason="trusted exact-candidate review",
            evidence_refs=EVIDENCE,
        ),
    )
    snapshot = coordinator.snapshot()
    plan_fingerprint = coordinator.trusted_plan_fingerprint

    restored = ProductFactoryCoordinator(_graph(), review_authority=authority)
    restored.restore(snapshot, trusted_plan_fingerprint=plan_fingerprint)

    record = _record(restored)
    assert record.state is WorkState.ACCEPTED
    assert record.result is not None
    assert record.result.producer_actor_id == PRODUCER
    assert len(authority.calls) == 2
    assert authority.calls[0].fingerprint == authority.calls[1].fingerprint


def test_secure_accepted_snapshot_fails_closed_without_authority() -> None:
    authority = _ExactAuthority()
    coordinator = _coordinator(authority)
    _record_secure_candidate(coordinator)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id=TRUSTED_REVIEWER,
            accepted=True,
            reason="trusted exact-candidate review",
            evidence_refs=EVIDENCE,
        ),
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="review authority is required"):
        restored.restore(
            coordinator.snapshot(),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
