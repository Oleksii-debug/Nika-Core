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
