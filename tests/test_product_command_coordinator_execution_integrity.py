from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import (
    ProductCommandCenter,
    ProductCommandCenterScopeError,
)
from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorSnapshot,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionRegistrySnapshot,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
    WorkLease,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec

SHA_A = "a" * 40
NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def _center(tmp_path) -> ProductCommandCenter:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    service.create_project(
        project_id="p1",
        name="Command center project",
        spec=ProductProjectSpec(
            goal="Build accessible product",
            desired_outcome="Tested product",
        ),
        idempotency_key="create:p1",
    )
    return ProductCommandCenter(service)


def _work_record(project_id: str = "p1", component_id: str = "core") -> WorkRecord:
    request = ComponentWorkRequest(
        work_id=f"work-{component_id}",
        project_id=project_id,
        component_id=component_id,
        repository_id="repo-core",
        goal="Implement component",
        base_sha=SHA_A,
        allowed_paths=("src/",),
        permission_ceiling=frozenset({"workspace.write"}),
        acceptance_commands=(("python", "-m", "pytest"),),
    )
    return WorkRecord(request, WorkState.BLOCKED, blocker="waiting for dependency")


def _node(node_id: str) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(node_id, Platform.WINDOWS, "x86_64", f"{node_id}-instance"),
        NodeCapabilities(frozenset({"package"}), frozenset({"python"})),
        ResourceEnvelope(4, 4096, 8192),
    )


def _lease(lease_id: str, project_id: str, work_id: str, node_id: str) -> WorkLease:
    return WorkLease(
        lease_id,
        project_id,
        work_id,
        node_id,
        NOW,
        NOW + timedelta(minutes=5),
    )


def test_pf2_state_is_project_scoped_and_blockers_are_recounted(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = CoordinatorSnapshot("p1", 1, (_work_record(),))

    detail = center.inspect_project("p1", coordinator=snapshot)
    component = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.COMPONENT
    )
    blocker = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER
    )

    assert component.item_id == "core"
    assert blocker.detail == "waiting for dependency"
    assert detail.summary.blocker_count == 1


def test_foreign_and_duplicate_coordinator_state_fail_closed(tmp_path) -> None:
    center = _center(tmp_path)

    with pytest.raises(ProductCommandCenterScopeError, match="different ProductProject"):
        center.inspect_project(
            "p1",
            coordinator=CoordinatorSnapshot("p2", 1, (_work_record("p2"),)),
        )

    record = _work_record()
    with pytest.raises(ProductCommandCenterScopeError, match="duplicate coordinator component"):
        center.inspect_project(
            "p1",
            coordinator=CoordinatorSnapshot("p1", 1, (record, record)),
        )


def test_worker_result_must_match_exact_request_identity(tmp_path) -> None:
    center = _center(tmp_path)
    base = _work_record()
    result = SimpleNamespace(
        work_id="wrong-work",
        component_id=base.request.component_id,
        repository_id=base.request.repository_id,
        base_sha=base.request.base_sha,
        coding_result=SimpleNamespace(job_id=base.request.work_id),
    )
    record = WorkRecord(base.request, WorkState.RUNNING, result=result)

    with pytest.raises(ProductCommandCenterScopeError, match="result evidence does not match"):
        center.inspect_project(
            "p1",
            coordinator=CoordinatorSnapshot("p1", 1, (record,)),
        )


def test_accepted_component_without_accepted_review_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    base = _work_record()
    result = SimpleNamespace(
        work_id=base.request.work_id,
        component_id=base.request.component_id,
        repository_id=base.request.repository_id,
        base_sha=base.request.base_sha,
        coding_result=SimpleNamespace(job_id=base.request.work_id),
    )
    record = WorkRecord(base.request, WorkState.ACCEPTED, result=result)

    with pytest.raises(ProductCommandCenterScopeError, match="accepted independent review"):
        center.inspect_project(
            "p1",
            coordinator=CoordinatorSnapshot("p1", 1, (record,)),
        )


def test_execution_presentation_filters_foreign_project_nodes(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = ExecutionRegistrySnapshot(
        (_node("node-a"), _node("node-b")),
        (
            _lease("lease-a", "p1", "work-a", "node-a"),
            _lease("lease-b", "p2", "work-b", "node-b"),
        ),
        3,
    )

    serialized = center.inspect_project("p1", execution=snapshot).model_dump_json()

    assert "node-a" in serialized
    assert "work-a" in serialized
    assert "node-b" not in serialized
    assert "work-b" not in serialized
    assert "p2" not in serialized


def test_execution_snapshot_duplicate_or_unknown_lease_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    lease = _lease("lease-a", "p1", "work-a", "node-a")

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate execution lease"):
        center.inspect_project(
            "p1",
            execution=ExecutionRegistrySnapshot((_node("node-a"),), (lease, lease), 2),
        )

    with pytest.raises(ProductCommandCenterScopeError, match="unknown node"):
        center.inspect_project(
            "p1",
            execution=ExecutionRegistrySnapshot(
                (_node("node-a"),),
                (_lease("lease-b", "p1", "work-b", "missing"),),
                2,
            ),
        )


def test_execution_snapshot_rejects_two_leases_on_one_node_and_bad_lifetime(
    tmp_path,
) -> None:
    center = _center(tmp_path)

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate leased execution node"):
        center.inspect_project(
            "p1",
            execution=ExecutionRegistrySnapshot(
                (_node("node-a"),),
                (
                    _lease("lease-a", "p1", "work-a", "node-a"),
                    _lease("lease-b", "p2", "work-b", "node-a"),
                ),
                3,
            ),
        )

    bad = WorkLease("lease-a", "p1", "work-a", "node-a", NOW, NOW)
    with pytest.raises(ProductCommandCenterScopeError, match="invalid lease lifetime"):
        center.inspect_project(
            "p1",
            execution=ExecutionRegistrySnapshot((_node("node-a"),), (bad,), 2),
        )
