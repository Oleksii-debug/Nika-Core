from __future__ import annotations

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.reference_safety import safe_evidence_reference
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionSnapshot,
    OperationState,
)
from nika_core.product_factory_deployment_waves import DeploymentWaveSnapshot
from nika_core.product_factory_fleet_maintenance import (
    NodeMaintenanceState,
    RollingMaintenanceSnapshot,
)
from nika_core.product_factory_operations import ProductOperationsSnapshot
from nika_core.product_factory_operations_contracts import ServiceHealth

_MAX_EVIDENCE = 20


def deployment_execution_status_entries(
    project_id: str,
    snapshot: DeploymentExecutionSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    entries: list[ProductStatusEntry] = []
    for record in snapshot.records:
        spec = record.spec
        if spec.intent.project_id != project_id:
            continue
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.DEPLOYMENT,
                item_id=f"deployment-operation:{spec.operation_id}",
                label=f"Deployment operation {spec.operation_id}",
                state=record.state.value,
                owner=record.node_id,
                detail=(
                    f"Environment: {spec.intent.environment.environment_id}; "
                    f"release SHA: {spec.intent.release.source_sha}; "
                    f"attempt: {record.attempt}."
                ),
                evidence=_evidence("deployment_operation", record.evidence_refs),
            )
        )
        if record.state in {
            OperationState.WAITING_FOR_NODE,
            OperationState.BLOCKED_CREDENTIAL,
            OperationState.RECONCILE_REQUIRED,
            OperationState.RECOVERY_REQUIRED,
            OperationState.REJECTED,
        }:
            entries.append(
                ProductStatusEntry(
                    kind=ProductStatusKind.BLOCKER,
                    item_id=f"deployment-operation:{spec.operation_id}:blocker",
                    label=f"Deployment operation blocked: {spec.operation_id}",
                    state="active",
                    detail=f"Execution-mediated deployment state: {record.state.value}.",
                    evidence=_evidence("deployment_operation", record.evidence_refs),
                )
            )
    return tuple(entries)


def deployment_wave_status_entries(
    project_id: str,
    snapshot: DeploymentWaveSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    entries: list[ProductStatusEntry] = []
    for record in snapshot.plans:
        if record.plan.project_id != project_id:
            continue
        service_state = ", ".join(
            f"{item.service_id}={item.state.value}" for item in record.services
        )
        evidence_refs = tuple(
            ref for item in record.services for ref in item.evidence_refs
        )
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.DEPLOYMENT,
                item_id=f"deployment-wave:{record.plan.plan_id}",
                label=f"Deployment wave plan {record.plan.plan_id}",
                state=record.state.value,
                detail=f"Services: {service_state}.",
                evidence=_evidence("deployment_wave", evidence_refs),
            )
        )
    return tuple(entries)


def product_operations_status_entries(
    project_id: str,
    snapshot: ProductOperationsSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    if snapshot.project_id != project_id:
        return ()
    entries: list[ProductStatusEntry] = []
    for record in snapshot.services:
        service = record.service
        evidence_refs: tuple[str, ...] = ()
        if record.observation is not None:
            evidence_refs += record.observation.evidence_refs
        if record.rollback is not None:
            evidence_refs += record.rollback.evidence_refs
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.DEPLOYMENT,
                item_id=f"service-health:{service.service_id}",
                label=f"Service health {service.service_id}",
                state=record.health.value,
                detail=(
                    f"Environment: {service.environment_id}; release SHA: "
                    f"{service.release_sha}; healthy threshold: "
                    f"{service.min_healthy_replicas}/{len(service.replicas)}; "
                    f"maintenance: {record.maintenance.value}; "
                    f"unavailable nodes: {len(record.node_loss)}."
                ),
                evidence=_evidence("service_health", evidence_refs),
            )
        )
        if record.health in {
            ServiceHealth.DEGRADED,
            ServiceHealth.FAILED,
            ServiceHealth.BLOCKED,
            ServiceHealth.ROLLBACK_REQUIRED,
        }:
            entries.append(
                ProductStatusEntry(
                    kind=ProductStatusKind.BLOCKER,
                    item_id=f"service-health:{service.service_id}:blocker",
                    label=f"Service requires attention: {service.service_id}",
                    state="active",
                    detail=(
                        f"Project-scoped service health is {record.health.value}; "
                        "inspect normalized PF3 evidence before resuming dependent work."
                    ),
                    evidence=_evidence("service_health", evidence_refs),
                )
            )
    return tuple(entries)


def rolling_maintenance_status_entries(
    project_id: str,
    snapshot: RollingMaintenanceSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    record_map = dict(snapshot.node_records)
    entries: list[ProductStatusEntry] = []
    for plan in snapshot.plans:
        if plan.project_id != project_id:
            continue
        nodes = record_map.get(plan.plan_id, ())
        states = tuple(record.state for record in nodes)
        state = _maintenance_plan_state(states)
        evidence_refs = plan.evidence_refs + tuple(
            ref for record in nodes for ref in record.evidence_refs
        )
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.DEPLOYMENT,
                item_id=f"rolling-maintenance:{plan.plan_id}",
                label=f"Rolling maintenance {plan.plan_id}",
                state=state,
                detail=(
                    f"Fleet plan: {plan.fleet_plan_id}; nodes: {len(plan.node_ids)}; "
                    f"node states: {', '.join(item.value for item in states) or 'none'}."
                ),
                evidence=_evidence("fleet_maintenance", evidence_refs),
            )
        )
        if state in {"blocked", "reconcile_required", "failed"}:
            entries.append(
                ProductStatusEntry(
                    kind=ProductStatusKind.BLOCKER,
                    item_id=f"rolling-maintenance:{plan.plan_id}:blocker",
                    label=f"Rolling maintenance blocked: {plan.plan_id}",
                    state="active",
                    detail=f"Rolling maintenance requires operator-safe handling: {state}.",
                    evidence=_evidence("fleet_maintenance", evidence_refs),
                )
            )
    return tuple(entries)


def _maintenance_plan_state(states: tuple[NodeMaintenanceState, ...]) -> str:
    if not states:
        return "invalid"
    if any(state is NodeMaintenanceState.FAILED for state in states):
        return "failed"
    if any(state is NodeMaintenanceState.RECONCILE_REQUIRED for state in states):
        return "reconcile_required"
    if any(
        state in {
            NodeMaintenanceState.BLOCKED_ACTIVE_LEASE,
            NodeMaintenanceState.BLOCKED_QUORUM,
            NodeMaintenanceState.BLOCKED_CREDENTIAL,
        }
        for state in states
    ):
        return "blocked"
    if all(state is NodeMaintenanceState.SUCCEEDED for state in states):
        return "succeeded"
    return "in_progress"


def _evidence(kind: str, refs: tuple[str, ...]) -> tuple[EvidenceReference, ...]:
    return tuple(
        EvidenceReference(
            kind=kind,
            reference=safe_evidence_reference(ref),
            label="Normalized Product Factory evidence",
        )
        for ref in refs[-_MAX_EVIDENCE:]
        if ref.strip()
    )
