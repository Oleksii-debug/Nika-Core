from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_command.factory_snapshot_safety import (
    ProductFactorySnapshotIntegrityError,
    validate_operations_projection,
    validate_rolling_maintenance_projection,
)
from nika_core.product_command.factory_status_adapter import (
    product_operations_status_entries,
    rolling_maintenance_status_entries,
)
from nika_core.product_factory_fleet_maintenance import (
    NodeMaintenanceAction,
    NodeMaintenanceRecord,
    NodeMaintenanceRequest,
    NodeMaintenanceState,
    RollingMaintenancePlan,
    RollingMaintenanceSnapshot,
    ServiceMaintenanceBinding,
)
from nika_core.product_factory_operations import (
    MaintenanceRecord,
    ProductOperationsSnapshot,
    ServiceRecord,
)
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)

NOW = datetime(2026, 8, 21, 12, 30, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "1" * 64


def _service(
    service_id: str = "service-api",
    *,
    wave: int = 0,
    dependencies: tuple[str, ...] = (),
    credential_refs: tuple[str, ...] = (),
) -> DeployableService:
    return DeployableService(
        service_id,
        "project-1",
        "stage",
        SHA,
        wave,
        (
            ServiceReplica(f"{service_id}-replica-1", "node-a"),
            ServiceReplica(f"{service_id}-replica-2", "node-b"),
        ),
        min_healthy_replicas=1,
        dependencies=dependencies,
        credential_refs=credential_refs,
    )


def _observation(
    service_id: str = "service-api",
    *,
    healthy: tuple[str, ...] = ("service-api-replica-1", "service-api-replica-2"),
    failed: tuple[str, ...] = (),
) -> ServiceObservation:
    return ServiceObservation(
        service_id,
        SHA,
        healthy,
        failed,
        ("health://project-1/service-api",),
        NOW,
    )


def test_operations_projection_rejects_forged_healthy_state() -> None:
    service = _service()
    observation = _observation(
        healthy=(),
        failed=("service-api-replica-1", "service-api-replica-2"),
    )
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(service, health=ServiceHealth.HEALTHY, observation=observation),),
        (),
        (),
        (),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="health disagrees with credential/observation/rollback state",
    ):
        product_operations_status_entries("project-1", snapshot)


def test_operations_projection_accepts_health_recomputed_from_observation() -> None:
    service = _service()
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.HEALTHY,
                observation=_observation(),
            ),
        ),
        (),
        (),
        (),
    )

    entries = product_operations_status_entries("project-1", snapshot)

    assert entries[0].state == "healthy"
    assert entries[0].evidence[0].reference == "health://project-1/service-api"


def test_operations_projection_rejects_dependency_not_in_earlier_wave() -> None:
    dependency = _service("service-db", wave=1)
    dependent = _service("service-api", wave=1, dependencies=("service-db",))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(dependency), ServiceRecord(dependent)),
        (),
        (),
        (),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="earlier wave",
    ):
        validate_operations_projection("project-1", snapshot)


def test_operations_projection_rejects_durable_maintenance_without_approval() -> None:
    service = _service()
    request = MaintenanceRequest(
        "maintenance-1",
        "service-api",
        MaintenanceAction.RESTART,
        "Restart unhealthy service",
        ("maintenance://requested",),
        approval_ref=None,
    )
    maintenance = MaintenanceRecord(
        request,
        MaintenanceResult(True, False, ("maintenance://applied",)),
    )
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(service),),
        (maintenance,),
        (),
        (),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="lacks explicit approval identity",
    ):
        validate_operations_projection("project-1", snapshot)


def _binding() -> ServiceMaintenanceBinding:
    return ServiceMaintenanceBinding(
        "service-api",
        "stage",
        SHA,
        DIGEST,
        ("service-api-replica-1",),
    )


def _plan() -> RollingMaintenancePlan:
    return RollingMaintenancePlan(
        "maintenance-plan-1",
        "project-1",
        "fleet-1",
        ("node-a",),
        "approval://project-1/maintenance-plan-1",
        "Patch execution node",
        ("maintenance://approved",),
    )


def test_rolling_projection_rejects_reconcile_without_durable_request() -> None:
    plan = _plan()
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.RECONCILE_REQUIRED,
        (_binding(),),
        cordoned=True,
    )
    snapshot = RollingMaintenanceSnapshot(
        (plan,),
        ((plan.plan_id, (record,)),),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="lacks durable pending request",
    ):
        rolling_maintenance_status_entries("project-1", snapshot)


def test_rolling_projection_rejects_forged_success_without_completed_actions() -> None:
    plan = _plan()
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.SUCCEEDED,
        (_binding(),),
        cordoned=False,
    )
    snapshot = RollingMaintenanceSnapshot(
        (plan,),
        ((plan.plan_id, (record,)),),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="state disagrees with completed actions",
    ):
        validate_rolling_maintenance_projection("project-1", snapshot)


def test_rolling_projection_rejects_pending_approval_action_or_reason_drift() -> None:
    plan = _plan()
    pending = NodeMaintenanceRequest(
        "maintenance-plan-1:node-a:restart",
        plan.plan_id,
        plan.project_id,
        plan.fleet_plan_id,
        "node-a",
        NodeMaintenanceAction.RESTART,
        (_binding(),),
        "Different reason",
        "approval://project-1/forged",
        plan.evidence_refs,
    )
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.RECONCILE_REQUIRED,
        (_binding(),),
        pending_request=pending,
        cordoned=True,
    )
    snapshot = RollingMaintenanceSnapshot(
        (plan,),
        ((plan.plan_id, (record,)),),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="plan/approval/action",
    ):
        validate_rolling_maintenance_projection("project-1", snapshot)


def test_valid_reconcile_checkpoint_preserves_exact_pending_request_binding() -> None:
    plan = _plan()
    pending = NodeMaintenanceRequest(
        "maintenance-plan-1:node-a:drain",
        plan.plan_id,
        plan.project_id,
        plan.fleet_plan_id,
        "node-a",
        NodeMaintenanceAction.DRAIN,
        (_binding(),),
        plan.reason,
        plan.approval_ref,
        plan.evidence_refs,
    )
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.RECONCILE_REQUIRED,
        (_binding(),),
        evidence_refs=("maintenance://provider-uncertain",),
        pending_request=pending,
        cordoned=True,
    )
    snapshot = RollingMaintenanceSnapshot(
        (plan,),
        ((plan.plan_id, (record,)),),
    )

    entries = rolling_maintenance_status_entries("project-1", snapshot)

    assert entries[0].state == "reconcile_required"
    assert any(item.state == "active" for item in entries[1:])
