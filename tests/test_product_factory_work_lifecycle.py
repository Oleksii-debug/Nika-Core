from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from tests.test_product_factory_coordinator import PERMISSIONS, SHA_A, _success


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-lifecycle",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent("core", "repo-1", ("src/core",)),
            ProductComponent("ui", "repo-1", ("src/ui",), dependencies=("core",)),
        ),
    )


def _coordinator() -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _core_record(coordinator: ProductFactoryCoordinator):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == "core"
    )


def test_done_work_is_terminal_and_never_resurrects_after_restore() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    done = coordinator.mark_done("core")
    assert done.state is WorkState.DONE
    assert "core" not in {item.component_id for item in coordinator.ready_requests()}

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    assert next(
        record.state for record in restored.snapshot().records if record.request.component_id == "core"
    ) is WorkState.DONE
    assert "core" not in {item.component_id for item in restored.ready_requests()}
    assert "ui" in {item.component_id for item in restored.ready_requests()}


def test_done_transition_is_idempotent_but_cannot_rebind_result() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    first = coordinator.mark_done("core")
    second = coordinator.mark_done("core")
    assert second == first
    with pytest.raises(CoordinatorError, match="done"):
        coordinator.start("core")


def test_cancelled_work_is_terminal_and_does_not_unlock_dependents() -> None:
    coordinator = _coordinator()
    cancelled = coordinator.cancel("core", reason="project scope changed")
    assert cancelled.state is WorkState.CANCELLED
    assert "core" not in {item.component_id for item in coordinator.ready_requests()}
    assert "ui" not in {item.component_id for item in coordinator.ready_requests()}

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    assert "core" not in {item.component_id for item in restored.ready_requests()}
    assert "ui" not in {item.component_id for item in restored.ready_requests()}


def test_cancel_is_idempotent_and_done_cannot_be_cancelled() -> None:
    coordinator = _coordinator()
    first = coordinator.cancel("core", reason="project scope changed")
    second = coordinator.cancel("core", reason="project scope changed")
    assert second == first

    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    coordinator.mark_done("core")
    with pytest.raises(CoordinatorError, match="done"):
        coordinator.cancel("core", reason="too late")


def test_accepted_work_cannot_be_cancelled_after_unlocking_dependents() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    assert "ui" in {item.component_id for item in coordinator.ready_requests()}

    with pytest.raises(CoordinatorError, match="accepted"):
        coordinator.cancel("core", reason="too late")

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    assert next(
        record.state for record in restored.snapshot().records if record.request.component_id == "core"
    ) is WorkState.ACCEPTED
    assert "ui" in {item.component_id for item in restored.ready_requests()}


def test_cancel_from_review_required_preserves_result_across_restart() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    review_required = coordinator.record_result(_success(request))
    assert review_required.state is WorkState.REVIEW_REQUIRED
    assert review_required.result is not None

    cancelled = coordinator.cancel("core", reason="scope removed after implementation")
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.result == review_required.result
    assert cancelled.review is None
    assert cancelled.blocker == "scope removed after implementation"
    assert coordinator.cancel("core", reason="scope removed after implementation") == cancelled
    with pytest.raises(CoordinatorError, match="reason cannot be rebound"):
        coordinator.cancel("core", reason="different cancellation reason")

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    restored_record = _core_record(restored)
    assert restored_record.state is WorkState.CANCELLED
    assert restored_record.result == review_required.result
    assert restored_record.result is not None
    assert restored_record.result.result_sha == review_required.result.result_sha
    assert restored_record.review is None
    assert restored_record.blocker == "scope removed after implementation"


def test_cancel_from_rejected_review_preserves_result_and_review_across_restart() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    review_required = coordinator.record_result(_success(request))
    decision = ReviewDecision("qa-1", False, "needs repair", ("ci:review-1",))
    repair_required = coordinator.review("core", decision)
    assert repair_required.state is WorkState.REPAIR_REQUIRED
    assert repair_required.result == review_required.result
    assert repair_required.review == decision

    cancelled = coordinator.cancel("core", reason="cancel instead of repairing")
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.result == repair_required.result
    assert cancelled.review == decision
    assert cancelled.blocker == "cancel instead of repairing"

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    restored_record = _core_record(restored)
    assert restored_record.state is WorkState.CANCELLED
    assert restored_record.result == repair_required.result
    assert restored_record.review == decision
    assert restored_record.blocker == "cancel instead of repairing"


def test_cancelled_snapshot_rejects_accepted_review_provenance() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    snapshot = coordinator.snapshot()
    records = tuple(
        replace(record, state=WorkState.CANCELLED, blocker="forged cancellation")
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="cancelled snapshot review evidence"):
        restored.restore(tampered, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)


@pytest.mark.parametrize("accepted", (1, "false", None))
def test_review_acceptance_requires_exact_boolean(accepted) -> None:
    with pytest.raises(CoordinatorError, match="exact boolean"):
        ReviewDecision("qa-1", accepted, "verified", ("ci:1",))


@pytest.mark.parametrize(
    "evidence_refs",
    (("",), ("   ",), (" ci:1",), ("ci:1\nforged",), ["ci:1"]),
)
def test_review_evidence_refs_require_canonical_text(evidence_refs) -> None:
    with pytest.raises(CoordinatorError, match="canonical text"):
        ReviewDecision("qa-1", True, "verified", evidence_refs)


@pytest.mark.parametrize("rejected_review", (False, True))
def test_block_rejects_post_result_provenance(rejected_review: bool) -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    result = coordinator.record_result(_success(request))
    if rejected_review:
        decision = ReviewDecision("qa-1", False, "needs repair", ("ci:review-1",))
        result = coordinator.review("core", decision)

    snapshot = coordinator.snapshot()
    with pytest.raises(CoordinatorError, match="result or review evidence"):
        coordinator.block("core", "external dependency")
    assert coordinator.snapshot() == snapshot
    assert _core_record(coordinator) == result


def test_running_work_cannot_be_cancelled_without_stop_and_fence_proof() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    snapshot = coordinator.snapshot()

    with pytest.raises(CoordinatorError, match="stop and fence proof"):
        coordinator.cancel("core", reason="stop requested")
    assert coordinator.snapshot() == snapshot


@pytest.mark.parametrize(
    "reason",
    (" first", "first ", "first\nRepair: second", "first\nsecond", "first\tsecond"),
)
def test_repair_reason_rejects_ambiguous_or_control_text(reason: str) -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", False, "needs repair", ("ci:1",)))
    snapshot = coordinator.snapshot()

    with pytest.raises(CoordinatorError, match="canonical single-line text"):
        coordinator.prepare_repair("core", base_sha=SHA_A, reason=reason)
    assert coordinator.snapshot() == snapshot
