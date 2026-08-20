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
    ReviewDecision,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNode,
    ExecutionRegistrySnapshot,
    HealthEvidence,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
    RollbackEvidence,
    WorkLease,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 8, 20, 19, 0, tzinfo=UTC)


def _center(tmp_path) -> ProductCommandCenter:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectCommandService(ProductProjectRepository(store))
    projects.create_project(
        project_id="project-1",
        name="Expense",
        spec=ProductProjectSpec(
            goal="Build accessible expense app",
            desired_outcome="Packaged accessible Windows application",
            repository_refs=("repo://expense/core",),
            team_refs=("role://accessibility",),
        ),
        idempotency_key="create:project-1",
    )
    return ProductCommandCenter(projects)


def _work_record(project_id: str = "project-1", component_id: str = "core") -> WorkRecord:
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


def _deployment_record(
    *,
    project_id: str,
    intent_id: str,
    environment_id: str,
    source_sha: str,
    digest: str,
) -> DeploymentRecord:
    intent = DeploymentIntent(
        intent_id,
        project_id,
        EnvironmentIdentity(
            environment_id,
            project_id,
            EnvironmentTier.STAGING,
            f"credential://provider/{project_id}",
        ),
        ReleaseRef(project_id, "1.0.0", source_sha, digest),
    )
    return DeploymentRecord(
        intent,
        DeploymentState.UNCERTAIN,
        (f"deploy://{project_id}/timeout",),
    )


def test_command_center_merges_project_scoped_pf2_state_and_recounts_blockers(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = CoordinatorSnapshot("project-1", 1, (_work_record(),))

    detail = center.inspect_project("project-1", coordinator=snapshot)

    component = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.COMPONENT
    )
    blocker = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER
    )
    assert component.item_id == "core"
    assert blocker.detail == "waiting for dependency"
    assert detail.summary.blocker_count == 1


def test_command_center_rejects_foreign_or_corrupt_coordinator_snapshot(tmp_path) -> None:
    center = _center(tmp_path)

    with pytest.raises(ProductCommandCenterScopeError, match="different ProductProject"):
        center.inspect_project(
            "project-1",
            coordinator=CoordinatorSnapshot("project-2", 1, (_work_record("project-2"),)),
        )

    with pytest.raises(ProductCommandCenterScopeError, match="cross-project work records"):
        center.inspect_project(
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (_work_record("project-2"),)),
        )


def test_duplicate_coordinator_component_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    record = _work_record()

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate component identities"):
        center.inspect_project(
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (record, record)),
        )


def test_duplicate_coordinator_work_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    first = _work_record(component_id="core")
    second_request = ComponentWorkRequest(
        work_id=first.request.work_id,
        project_id="project-1",
        component_id="ui",
        repository_id="repo-core",
        goal="Implement UI",
        base_sha=SHA_A,
        allowed_paths=("ui/",),
        permission_ceiling=frozenset({"workspace.write"}),
        acceptance_commands=(("python", "-m", "pytest"),),
    )
    second = WorkRecord(second_request, WorkState.READY)

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate work identities"):
        center.inspect_project(
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (first, second)),
        )


def test_coordinator_result_identity_mismatch_fails_closed(tmp_path) -> None:
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
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (record,)),
        )


def test_review_without_worker_result_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    base = _work_record()
    review = ReviewDecision("auditor", True, "approved", ("review://ok",))
    record = WorkRecord(base.request, WorkState.REVIEW_REQUIRED, review=review)

    with pytest.raises(ProductCommandCenterScopeError, match="review exists without"):
        center.inspect_project(
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (record,)),
        )


def test_accepted_work_without_accepted_review_evidence_fails_closed(tmp_path) -> None:
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
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (record,)),
        )


def test_execution_presentation_only_exposes_nodes_leased_to_target_project(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = ExecutionRegistrySnapshot(
        (_node("node-a"), _node("node-b")),
        (
            _lease("lease-a", "project-1", "work-a", "node-a"),
            _lease("lease-b", "project-2", "work-b", "node-b"),
        ),
        3,
    )

    detail = center.inspect_project("project-1", execution=snapshot)
    serialized = detail.model_dump_json()

    assert "execution-node:node-a" in serialized
    assert "work-a" in serialized
    assert "node-b" not in serialized
    assert "work-b" not in serialized
    assert "project-2" not in serialized


def test_duplicate_execution_node_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = ExecutionRegistrySnapshot((_node("node-a"), _node("node-a")), (), 1)

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate node identities"):
        center.inspect_project("project-1", execution=snapshot)


def test_duplicate_execution_lease_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    lease = _lease("lease-a", "project-1", "work-a", "node-a")
    snapshot = ExecutionRegistrySnapshot((_node("node-a"),), (lease, lease), 2)

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate lease identities"):
        center.inspect_project("project-1", execution=snapshot)


def test_execution_node_cannot_have_two_active_leases(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = ExecutionRegistrySnapshot(
        (_node("node-a"),),
        (
            _lease("lease-a", "project-1", "work-a", "node-a"),
            _lease("lease-b", "project-2", "work-b", "node-a"),
        ),
        3,
    )

    with pytest.raises(ProductCommandCenterScopeError, match="multiple active leases"):
        center.inspect_project("project-1", execution=snapshot)


def test_execution_lease_unknown_node_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = ExecutionRegistrySnapshot(
        (_node("node-a"),),
        (_lease("lease-b", "project-1", "work-b", "missing-node"),),
        2,
    )

    with pytest.raises(ProductCommandCenterScopeError, match="unknown node"):
        center.inspect_project("project-1", execution=snapshot)


def test_execution_lease_invalid_lifetime_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    bad = WorkLease("lease-a", "project-1", "work-a", "node-a", NOW, NOW)
    snapshot = ExecutionRegistrySnapshot((_node("node-a"),), (bad,), 2)

    with pytest.raises(ProductCommandCenterScopeError, match="invalid lease lifetime"):
        center.inspect_project("project-1", execution=snapshot)


def test_deployment_presentation_filters_foreign_project_records(tmp_path) -> None:
    center = _center(tmp_path)
    target = _deployment_record(
        project_id="project-1",
        intent_id="stage-target",
        environment_id="env-target",
        source_sha=SHA_A,
        digest=DIGEST_A,
    )
    foreign = _deployment_record(
        project_id="project-2",
        intent_id="stage-foreign",
        environment_id="env-foreign",
        source_sha=SHA_B,
        digest=DIGEST_B,
    )
    snapshot = DeploymentFabricSnapshot(
        (target, foreign),
        (("project-1", SHA_A), ("project-2", SHA_B)),
        (("env-target", SHA_A), ("env-foreign", SHA_B)),
    )

    detail = center.inspect_project("project-1", deployment=snapshot)
    serialized = detail.model_dump_json()

    assert "stage-target" in serialized
    assert SHA_A in serialized
    assert "stage-foreign" not in serialized
    assert SHA_B not in serialized
    assert "project-2" not in serialized
    assert "credential://provider/" not in serialized


def test_duplicate_deployment_intent_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    record = _deployment_record(
        project_id="project-1",
        intent_id="stage-target",
        environment_id="env-target",
        source_sha=SHA_A,
        digest=DIGEST_A,
    )
    snapshot = DeploymentFabricSnapshot((record, record), (), ())

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate intent identities"):
        center.inspect_project("project-1", deployment=snapshot)


def test_duplicate_healthy_staging_project_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (),
        (("project-1", SHA_A), ("project-1", SHA_B)),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="healthy-staging"):
        center.inspect_project("project-1", deployment=snapshot)


def test_duplicate_current_release_environment_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (),
        (),
        (("env-target", SHA_A), ("env-target", SHA_B)),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="current-release"):
        center.inspect_project("project-1", deployment=snapshot)


def test_mismatched_deployment_health_evidence_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    base = _deployment_record(
        project_id="project-1",
        intent_id="stage-target",
        environment_id="env-target",
        source_sha=SHA_A,
        digest=DIGEST_A,
    )
    health = HealthEvidence("env-other", SHA_A, True, ("health://ok",), NOW)
    record = DeploymentRecord(
        base.intent,
        DeploymentState.HEALTH_CHECK,
        ("deploy://ok",),
        health=health,
    )

    with pytest.raises(ProductCommandCenterScopeError, match="health evidence does not match"):
        center.inspect_project(
            "project-1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )


def test_healthy_deployment_state_requires_healthy_evidence(tmp_path) -> None:
    center = _center(tmp_path)
    base = _deployment_record(
        project_id="project-1",
        intent_id="stage-target",
        environment_id="env-target",
        source_sha=SHA_A,
        digest=DIGEST_A,
    )
    record = DeploymentRecord(base.intent, DeploymentState.HEALTHY, ("deploy://ok",))

    with pytest.raises(ProductCommandCenterScopeError, match="lacks matching healthy evidence"):
        center.inspect_project(
            "project-1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )


def test_mismatched_rollback_evidence_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    base = _deployment_record(
        project_id="project-1",
        intent_id="stage-target",
        environment_id="env-target",
        source_sha=SHA_A,
        digest=DIGEST_A,
    )
    rollback = RollbackEvidence("env-target", SHA_B, None, False, ("rollback://bad",))
    record = DeploymentRecord(
        base.intent,
        DeploymentState.REJECTED,
        ("deploy://ok",),
        rollback=rollback,
    )

    with pytest.raises(ProductCommandCenterScopeError, match="rollback evidence does not match"):
        center.inspect_project(
            "project-1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )


def test_rolled_back_state_requires_successful_rollback_evidence(tmp_path) -> None:
    center = _center(tmp_path)
    base = _deployment_record(
        project_id="project-1",
        intent_id="stage-target",
        environment_id="env-target",
        source_sha=SHA_A,
        digest=DIGEST_A,
    )
    record = DeploymentRecord(base.intent, DeploymentState.ROLLED_BACK, ("deploy://ok",))

    with pytest.raises(ProductCommandCenterScopeError, match="successful rollback evidence"):
        center.inspect_project(
            "project-1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )
