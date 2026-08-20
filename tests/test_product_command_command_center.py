from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNode,
    ExecutionRegistrySnapshot,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
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


def test_duplicate_project_status_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    record = _work_record()

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate ProductProject status"):
        center.inspect_project(
            "project-1",
            coordinator=CoordinatorSnapshot("project-1", 1, (record, record)),
        )
