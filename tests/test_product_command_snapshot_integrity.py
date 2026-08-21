from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_command.command_center import (
    ProductCommandCenterScopeError,
    _validate_deployment_execution_snapshot,
    _validate_deployment_wave_snapshot,
    _validate_operations_scope,
    _validate_rolling_maintenance_snapshot,
)
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionRequest,
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
from nika_core.product_factory_deployment_waves import (
    DeploymentWavePlan,
    DeploymentWaveRecord,
    DeploymentWaveSnapshot,
    RolloutState,
    ServiceRolloutRecord,
    ServiceRolloutSpec,
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
from nika_core.product_factory_operations import ProductOperationsSnapshot, ServiceRecord
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceHealth,
    ServiceReplica,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
SHA_A = "a" * 40
DIGEST_A = "1" * 64


def _execution_spec(
    *,
    project_id: str = "project-1",
    operation_id: str = "operation-1",
) -> DeploymentExecutionSpec:
    intent = DeploymentIntent(
        f"intent:{operation_id}",
        project_id,
        EnvironmentIdentity(
            "stage",
            project_id,
            EnvironmentTier.STAGING,
            f"credential://provider/{project_id}/writer",
        ),
        ReleaseRef(project_id, "1.0.0", SHA_A, DIGEST_A),
    )
    return DeploymentExecutionSpec(
        operation_id,
        ExecutionRequest(
            project_id,
            f"work:{operation_id}",
            Platform.WINDOWS,
            frozenset({"deploy"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        intent,
        f"credential://provider/{project_id}/writer",
        "staging-provider",
        "deploy",
    )


def _execution_record(
    state: OperationState,
    *,
    spec: DeploymentExecutionSpec | None = None,
    node_id: str | None = None,
    attempt: int = 1,
    evidence_refs: tuple[str, ...] = (),
    deployment_state: DeploymentState | None = None,
) -> DeploymentExecutionRecord:
    return DeploymentExecutionRecord(
        spec or _execution_spec(),
        state,
        node_id=node_id,
        deployment_state=deployment_state,
        evidence_refs=evidence_refs,
        attempt=attempt,
        updated_at=NOW,
    )


def _wave_snapshot(
    execution: DeploymentExecutionRecord,
    *,
    rollout_state: RolloutState,
    service_state: OperationState | None = None,
    service_id: str = "service-api",
    service_operation_id: str | None = None,
    service_wave: int = 0,
    service_evidence: tuple[str, ...] | None = None,
    plan_spec: DeploymentExecutionSpec | None = None,
) -> DeploymentWaveSnapshot:
    planned_execution = plan_spec or execution.spec
    plan = DeploymentWavePlan(
        "wave-plan-1",
        planned_execution.intent.project_id,
        (ServiceRolloutSpec("service-api", 0, planned_execution),),
    )
    service = ServiceRolloutRecord(
        service_id,
        service_operation_id or execution.spec.operation_id,
        service_wave,
        execution.state if service_state is None else service_state,
        execution.attempt,
        execution.evidence_refs if service_evidence is None else service_evidence,
    )
    return DeploymentWaveSnapshot(
        (DeploymentWaveRecord(plan, rollout_state, (service,)),),
        DeploymentExecutionSnapshot((execution,)),
    )


def _service(
    *,
    dependencies: tuple[str, ...] = (),
    credential_refs: tuple[str, ...] = (),
) -> DeployableService:
    return DeployableService(
        "service-api",
        "project-1",
        "stage",
        SHA_A,
        0,
        (ServiceReplica("replica-1", "node-a"),),
        min_healthy_replicas=1,
        dependencies=dependencies,
        credential_refs=credential_refs,
    )


def test_execution_snapshot_rejects_serialized_active_node_lease() -> None:
    snapshot = DeploymentExecutionSnapshot(
        (_execution_record(OperationState.WAITING_FOR_NODE, node_id="node-a"),)
    )

    with pytest.raises(ProductCommandCenterScopeError, match="active ephemeral lease"):
        _validate_deployment_execution_snapshot(snapshot)


def test_execution_snapshot_rejects_prepared_restart_state() -> None:
    snapshot = DeploymentExecutionSnapshot((_execution_record(OperationState.PREPARED),))

    with pytest.raises(ProductCommandCenterScopeError, match="active ephemeral lease"):
        _validate_deployment_execution_snapshot(snapshot)


def test_execution_snapshot_rejects_terminal_deployment_state_mismatch() -> None:
    snapshot = DeploymentExecutionSnapshot(
        (
            _execution_record(
                OperationState.SUCCEEDED,
                deployment_state=DeploymentState.REJECTED,
                evidence_refs=("deployment://forged",),
            ),
        )
    )

    with pytest.raises(ProductCommandCenterScopeError, match="disagrees with deployment fabric"):
        _validate_deployment_execution_snapshot(snapshot)


def test_wave_snapshot_rejects_service_state_forged_against_execution() -> None:
    execution = _execution_record(
        OperationState.WAITING_FOR_NODE,
        attempt=2,
        evidence_refs=("execution://waiting",),
    )
    snapshot = _wave_snapshot(
        execution,
        rollout_state=RolloutState.SUCCEEDED,
        service_state=OperationState.SUCCEEDED,
    )

    with pytest.raises(ProductCommandCenterScopeError, match="disagrees with execution snapshot"):
        _validate_deployment_wave_snapshot(snapshot)


def test_wave_snapshot_rejects_forged_rollout_summary_state() -> None:
    execution = _execution_record(
        OperationState.WAITING_FOR_NODE,
        evidence_refs=("execution://waiting",),
    )
    snapshot = _wave_snapshot(execution, rollout_state=RolloutState.SUCCEEDED)

    with pytest.raises(ProductCommandCenterScopeError, match="summary state disagrees"):
        _validate_deployment_wave_snapshot(snapshot)


def test_wave_snapshot_rejects_service_operation_binding_drift() -> None:
    execution = _execution_record(OperationState.PENDING)
    snapshot = _wave_snapshot(
        execution,
        rollout_state=RolloutState.PENDING,
        service_id="service-forged",
    )

    with pytest.raises(ProductCommandCenterScopeError, match="service binding drifted"):
        _validate_deployment_wave_snapshot(snapshot)


def test_wave_snapshot_rejects_plan_payload_different_from_execution_snapshot() -> None:
    execution = _execution_record(
        OperationState.WAITING_FOR_NODE,
        spec=_execution_spec(project_id="project-2", operation_id="operation-shared"),
    )
    plan_spec = _execution_spec(project_id="project-1", operation_id="operation-shared")
    snapshot = _wave_snapshot(
        execution,
        rollout_state=RolloutState.PAUSED,
        plan_spec=plan_spec,
    )

    with pytest.raises(ProductCommandCenterScopeError, match="payload disagrees"):
        _validate_deployment_wave_snapshot(snapshot)


def test_valid_waiting_wave_snapshot_preserves_restart_parity() -> None:
    execution = _execution_record(
        OperationState.WAITING_FOR_NODE,
        attempt=3,
        evidence_refs=("execution://waiting",),
    )
    snapshot = _wave_snapshot(execution, rollout_state=RolloutState.PAUSED)

    _validate_deployment_wave_snapshot(snapshot)


def test_operations_snapshot_rejects_missing_service_dependency() -> None:
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(_service(dependencies=("service-db",))),),
        (),
        (),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="dependency is missing"):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_requires_revocation_for_credential_blocker() -> None:
    secret_ref = "credential://provider/project-1/writer"
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                _service(credential_refs=(secret_ref,)),
                health=ServiceHealth.BLOCKED,
                blocked_credentials=(secret_ref,),
            ),
        ),
        (),
        (),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="lacks revocation state"):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_rejects_node_loss_drift() -> None:
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(_service()),),
        (),
        (),
        ("node-a",),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="node-loss evidence disagrees"):
        _validate_operations_scope("project-1", snapshot)


def test_valid_credential_blocked_operations_snapshot_passes() -> None:
    secret_ref = "credential://provider/project-1/writer"
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                _service(credential_refs=(secret_ref,)),
                health=ServiceHealth.BLOCKED,
                blocked_credentials=(secret_ref,),
            ),
        ),
        (),
        (secret_ref,),
        (),
    )

    _validate_operations_scope("project-1", snapshot)


def _binding(replica_id: str = "replica-1") -> ServiceMaintenanceBinding:
    return ServiceMaintenanceBinding(
        "service-api",
        "stage",
        SHA_A,
        DIGEST_A,
        (replica_id,),
    )


def test_rolling_maintenance_rejects_foreign_project_snapshot() -> None:
    plan = RollingMaintenancePlan(
        "maintenance-1",
        "project-2",
        "fleet-1",
        ("node-a",),
        "approval://project-2/maintenance",
        "Patch node",
        ("maintenance://approved",),
    )
    node = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.PENDING,
        (_binding(),),
    )
    snapshot = RollingMaintenanceSnapshot(
        (plan,),
        (("maintenance-1", (node,)),),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="different ProductProject"):
        _validate_rolling_maintenance_snapshot("project-1", snapshot)


def test_rolling_maintenance_rejects_pending_request_binding_drift() -> None:
    plan = RollingMaintenancePlan(
        "maintenance-1",
        "project-1",
        "fleet-1",
        ("node-a",),
        "approval://project-1/maintenance",
        "Patch node",
        ("maintenance://approved",),
    )
    pending = NodeMaintenanceRequest(
        "maintenance-1:node-a:drain",
        "maintenance-1",
        "project-1",
        "fleet-1",
        "node-a",
        NodeMaintenanceAction.DRAIN,
        (_binding("replica-forged"),),
        "Patch node",
        "approval://project-1/maintenance",
        ("maintenance://approved",),
    )
    node = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.RECONCILE_REQUIRED,
        (_binding(),),
        pending_request=pending,
        cordoned=True,
    )
    snapshot = RollingMaintenanceSnapshot(
        (plan,),
        (("maintenance-1", (node,)),),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="plan/binding identity"):
        _validate_rolling_maintenance_snapshot("project-1", snapshot)
