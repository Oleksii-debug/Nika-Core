from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import (
    ProductCommandCenter,
    ProductCommandCenterScopeError,
)
from nika_core.product_command.deployment_adapter import (
    DeploymentPresentationIntegrityError,
)
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionRequest,
    HealthEvidence,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionRecord,
    DeploymentExecutionSnapshot,
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_factory_fleet_maintenance import (
    NodeMaintenanceRecord,
    NodeMaintenanceState,
    RollingMaintenancePlan,
    RollingMaintenanceSnapshot,
    ServiceMaintenanceBinding,
)
from nika_core.product_factory_operations import ProductOperationsSnapshot, ServiceRecord
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceHealth,
    ServiceReplica,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
SECRET_REF = "credential://provider/project-1/DO-NOT-LEAK"
APPROVAL_REF = "approval://project-1/DO-NOT-LEAK"


def _center(tmp_path) -> ProductCommandCenter:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    service.create_project(
        project_id="project-1",
        name="Accessible Product",
        spec=ProductProjectSpec(
            goal="Build a durable accessible product",
            desired_outcome="Qualified project-scoped release",
            credential_refs=(SECRET_REF,),
        ),
        idempotency_key="create:project-1",
    )
    return ProductCommandCenter(service)


def _intent(
    project_id: str,
    *,
    intent_id: str,
    environment_id: str = "shared-stage",
    sha: str = SHA_A,
    digest: str = DIGEST_A,
) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        project_id,
        EnvironmentIdentity(
            environment_id,
            project_id,
            EnvironmentTier.STAGING,
            f"credential://provider/{project_id}/writer",
        ),
        ReleaseRef(project_id, "1.0.0", sha, digest),
    )


def _healthy_record(
    project_id: str,
    *,
    intent_id: str,
    environment_id: str = "shared-stage",
    sha: str = SHA_A,
    digest: str = DIGEST_A,
) -> DeploymentRecord:
    intent = _intent(
        project_id,
        intent_id=intent_id,
        environment_id=environment_id,
        sha=sha,
        digest=digest,
    )
    return DeploymentRecord(
        intent,
        DeploymentState.HEALTHY,
        (f"deploy://{project_id}/{intent_id}",),
        health=HealthEvidence(
            environment_id,
            sha,
            True,
            (f"health://{project_id}/{intent_id}",),
            NOW,
        ),
    )


def test_current_release_identity_is_scoped_by_project_and_environment(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (
            _healthy_record("project-1", intent_id="deploy-p1", sha=SHA_A),
            _healthy_record(
                "project-2",
                intent_id="deploy-p2",
                sha=SHA_B,
                digest=DIGEST_B,
            ),
        ),
        (),
        (
            ("project-1", "shared-stage", SHA_A),
            ("project-2", "shared-stage", SHA_B),
        ),
    )

    detail = center.inspect_project("project-1", deployment=snapshot)
    serialized = detail.model_dump_json()

    assert "deploy-p1" in serialized
    assert "deploy-p2" not in serialized
    assert SHA_A in serialized
    assert SHA_B not in serialized
    assert "project-2" not in serialized


def test_legacy_environment_only_current_release_fails_closed_when_ambiguous(
    tmp_path,
) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (
            _healthy_record("project-1", intent_id="deploy-p1", sha=SHA_A),
            _healthy_record(
                "project-2",
                intent_id="deploy-p2",
                sha=SHA_B,
                digest=DIGEST_B,
            ),
        ),
        (),
        (("shared-stage", SHA_A),),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="ambiguous across projects"):
        center.inspect_project("project-1", deployment=snapshot)


def test_duplicate_project_environment_current_release_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (_healthy_record("project-1", intent_id="deploy-p1"),),
        (),
        (
            ("project-1", "shared-stage", SHA_A),
            ("project-1", "shared-stage", SHA_B),
        ),
    )

    with pytest.raises(
        ProductCommandCenterScopeError,
        match="duplicate current-release project/environment",
    ):
        center.inspect_project("project-1", deployment=snapshot)


def test_forged_current_release_marker_without_healthy_record_fails_closed(
    tmp_path,
) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (_healthy_record("project-1", intent_id="deploy-p1", sha=SHA_A),),
        (),
        (("project-1", "shared-stage", SHA_C),),
    )

    with pytest.raises(DeploymentPresentationIntegrityError, match="not backed"):
        center.inspect_project("project-1", deployment=snapshot)


def test_forged_healthy_staging_marker_without_healthy_record_fails_closed(
    tmp_path,
) -> None:
    center = _center(tmp_path)
    snapshot = DeploymentFabricSnapshot(
        (_healthy_record("project-1", intent_id="deploy-p1", sha=SHA_A),),
        (("project-1", SHA_C),),
        (("project-1", "shared-stage", SHA_A),),
    )

    with pytest.raises(DeploymentPresentationIntegrityError, match="healthy staging"):
        center.inspect_project("project-1", deployment=snapshot)


def test_execution_mediated_status_is_project_scoped_and_redacts_credential(
    tmp_path,
) -> None:
    center = _center(tmp_path)
    target_spec = DeploymentExecutionSpec(
        "operation-p1",
        ExecutionRequest(
            "project-1",
            "work-p1",
            Platform.WINDOWS,
            frozenset({"deploy"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        _intent("project-1", intent_id="exec-deploy-p1"),
        SECRET_REF,
        "staging-provider",
        "deploy",
    )
    foreign_spec = DeploymentExecutionSpec(
        "operation-p2",
        ExecutionRequest(
            "project-2",
            "work-p2",
            Platform.WINDOWS,
            frozenset({"deploy"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        _intent(
            "project-2",
            intent_id="exec-deploy-p2",
            sha=SHA_B,
            digest=DIGEST_B,
        ),
        "credential://provider/project-2/DO-NOT-LEAK",
        "staging-provider",
        "deploy",
    )
    snapshot = DeploymentExecutionSnapshot(
        (
            DeploymentExecutionRecord(
                target_spec,
                OperationState.BLOCKED_CREDENTIAL,
                evidence_refs=("execution://credential-blocked",),
                attempt=2,
                updated_at=NOW,
            ),
            DeploymentExecutionRecord(
                foreign_spec,
                OperationState.SUCCEEDED,
                evidence_refs=("execution://foreign",),
                attempt=1,
                updated_at=NOW,
            ),
        )
    )

    detail = center.inspect_project("project-1", deployment_execution=snapshot)
    serialized = detail.model_dump_json()

    operation = next(
        item
        for item in detail.statuses
        if item.item_id == "deployment-operation:operation-p1"
    )
    assert operation.state == "blocked_credential"
    assert detail.summary.blocker_count == 1
    assert "operation-p2" not in serialized
    assert "credential://" not in serialized
    assert SECRET_REF not in serialized


def test_product_operations_blocker_is_scoped_without_credential_disclosure(
    tmp_path,
) -> None:
    center = _center(tmp_path)
    service = DeployableService(
        "service-api",
        "project-1",
        "shared-stage",
        SHA_A,
        0,
        (
            ServiceReplica("replica-1", "node-a"),
            ServiceReplica("replica-2", "node-b"),
        ),
        min_healthy_replicas=1,
        credential_refs=(SECRET_REF,),
    )
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.BLOCKED,
                blocked_credentials=(SECRET_REF,),
            ),
        ),
        (),
        (SECRET_REF,),
        (),
    )

    detail = center.inspect_project("project-1", operations=snapshot)
    serialized = detail.model_dump_json()

    service_status = next(
        item for item in detail.statuses if item.item_id == "service-health:service-api"
    )
    assert service_status.state == "blocked"
    assert detail.summary.blocker_count == 1
    assert "credential://" not in serialized
    assert SECRET_REF not in serialized


def test_foreign_operations_snapshot_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)

    with pytest.raises(ProductCommandCenterScopeError, match="different ProductProject"):
        center.inspect_project(
            "project-1",
            operations=ProductOperationsSnapshot("project-2", (), (), (), ()),
        )


def test_rolling_maintenance_blocker_redacts_approval_material(tmp_path) -> None:
    center = _center(tmp_path)
    binding = ServiceMaintenanceBinding(
        "service-api",
        "shared-stage",
        SHA_A,
        DIGEST_A,
        ("replica-1",),
    )
    plan = RollingMaintenancePlan(
        "maintenance-1",
        "project-1",
        "fleet-1",
        ("node-a",),
        APPROVAL_REF,
        "Patch execution node",
        ("maintenance://approved",),
    )
    node = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.BLOCKED_ACTIVE_LEASE,
        (binding,),
        evidence_refs=("execution-node:cordoned:node-a",),
        cordoned=True,
    )

    detail = center.inspect_project(
        "project-1",
        fleet_maintenance=RollingMaintenanceSnapshot(
            (plan,),
            (("maintenance-1", (node,)),),
        ),
    )
    serialized = detail.model_dump_json()

    maintenance = next(
        item
        for item in detail.statuses
        if item.item_id == "rolling-maintenance:maintenance-1"
    )
    assert maintenance.state == "blocked"
    assert detail.summary.blocker_count == 1
    assert APPROVAL_REF not in serialized
    assert "approval://" not in serialized
    assert SECRET_REF not in serialized


def test_rolling_maintenance_checkpoint_plan_mismatch_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    plan = RollingMaintenancePlan(
        "maintenance-1",
        "project-1",
        "fleet-1",
        ("node-a",),
        APPROVAL_REF,
        "Patch execution node",
        ("maintenance://approved",),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="checkpoint set"):
        center.inspect_project(
            "project-1",
            fleet_maintenance=RollingMaintenanceSnapshot((plan,), ()),
        )
