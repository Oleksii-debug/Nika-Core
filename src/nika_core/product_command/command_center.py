from __future__ import annotations

from collections.abc import Iterable

from nika_core.product_command.contracts import ProductProjectDetail, ProductStatusKind
from nika_core.product_command.coordinator_adapter import coordinator_status_entries
from nika_core.product_command.credential_adapter import credential_status_entries
from nika_core.product_command.deployment_adapter import (
    deployment_status_entries,
    execution_status_entries,
)
from nika_core.product_command.factory_status_adapter import (
    deployment_execution_status_entries,
    deployment_wave_status_entries,
    product_operations_status_entries,
    rolling_maintenance_status_entries,
)
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_coordinator import CoordinatorSnapshot, WorkState
from nika_core.product_factory_credentials import CredentialBrokerSnapshot
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentState,
    ExecutionRegistrySnapshot,
)
from nika_core.product_factory_deployment_execution import DeploymentExecutionSnapshot
from nika_core.product_factory_deployment_waves import DeploymentWaveSnapshot
from nika_core.product_factory_fleet_maintenance import RollingMaintenanceSnapshot
from nika_core.product_factory_operations import ProductOperationsSnapshot


class ProductCommandCenterScopeError(ValueError):
    """Raised when a Product Factory snapshot cannot be safely scoped to one project."""


class ProductCommandCenter:
    """Compose PF1/PF2/PF3 presentation without leaking or misbinding project state."""

    def __init__(self, projects: ProductProjectCommandService) -> None:
        self._projects = projects

    def inspect_project(
        self,
        project_id: str,
        *,
        coordinator: CoordinatorSnapshot | None = None,
        execution: ExecutionRegistrySnapshot | None = None,
        deployment: DeploymentFabricSnapshot | None = None,
        credentials: CredentialBrokerSnapshot | None = None,
        deployment_execution: DeploymentExecutionSnapshot | None = None,
        deployment_waves: DeploymentWaveSnapshot | None = None,
        operations: ProductOperationsSnapshot | None = None,
        fleet_maintenance: RollingMaintenanceSnapshot | None = None,
    ) -> ProductProjectDetail:
        detail, credential_refs = self._projects.inspect_project_context(project_id)
        statuses = list(detail.statuses)

        if coordinator is not None:
            _validate_coordinator_scope(project_id, coordinator)
            statuses.extend(coordinator_status_entries(coordinator))
        if execution is not None:
            _validate_execution_snapshot(execution)
            statuses.extend(execution_status_entries(_scope_execution(project_id, execution)))
        if deployment is not None:
            release_identities = _validate_deployment_snapshot(deployment)
            statuses.extend(
                deployment_status_entries(
                    _scope_deployment(project_id, deployment, release_identities)
                )
            )
        if credentials is not None:
            _validate_credential_scope(project_id, credential_refs, credentials)
            statuses.extend(
                credential_status_entries(project_id, credential_refs, credentials)
            )
        if deployment_execution is not None:
            _validate_deployment_execution_snapshot(deployment_execution)
            statuses.extend(
                deployment_execution_status_entries(project_id, deployment_execution)
            )
        if deployment_waves is not None:
            _validate_deployment_wave_snapshot(deployment_waves)
            statuses.extend(deployment_wave_status_entries(project_id, deployment_waves))
        if operations is not None:
            _validate_operations_scope(project_id, operations)
            statuses.extend(product_operations_status_entries(project_id, operations))
        if fleet_maintenance is not None:
            _validate_rolling_maintenance_snapshot(fleet_maintenance)
            statuses.extend(
                rolling_maintenance_status_entries(project_id, fleet_maintenance)
            )

        _require_unique_status_identity(statuses)
        blocker_count = sum(item.kind is ProductStatusKind.BLOCKER for item in statuses)
        summary = detail.summary.model_copy(update={"blocker_count": blocker_count})
        return detail.model_copy(
            update={
                "summary": summary,
                "statuses": tuple(statuses),
            }
        )


def _validate_coordinator_scope(
    project_id: str,
    snapshot: CoordinatorSnapshot,
) -> None:
    if snapshot.project_id != project_id:
        raise ProductCommandCenterScopeError(
            "coordinator snapshot belongs to a different ProductProject"
        )
    component_ids = [record.request.component_id for record in snapshot.records]
    work_ids = [record.request.work_id for record in snapshot.records]
    _require_unique(component_ids, "coordinator component")
    _require_unique(work_ids, "coordinator work")
    for record in snapshot.records:
        request = record.request
        if request.project_id != project_id:
            raise ProductCommandCenterScopeError(
                "coordinator snapshot contains cross-project work records"
            )
        result = record.result
        if result is not None and (
            result.work_id != request.work_id
            or result.component_id != request.component_id
            or result.repository_id != request.repository_id
            or result.base_sha != request.base_sha
            or result.coding_result.job_id != request.work_id
        ):
            raise ProductCommandCenterScopeError(
                "coordinator result evidence does not match its work request"
            )
        if record.review is not None and result is None:
            raise ProductCommandCenterScopeError(
                "coordinator review exists without worker result evidence"
            )
        if record.state is WorkState.REVIEW_REQUIRED and result is None:
            raise ProductCommandCenterScopeError(
                "review-required coordinator record lacks worker result evidence"
            )
        if record.state is WorkState.ACCEPTED and (
            result is None or record.review is None or not record.review.accepted
        ):
            raise ProductCommandCenterScopeError(
                "accepted coordinator record lacks accepted independent review evidence"
            )


def _validate_execution_snapshot(snapshot: ExecutionRegistrySnapshot) -> None:
    node_ids = [node.identity.node_id for node in snapshot.nodes]
    lease_ids = [lease.lease_id for lease in snapshot.leases]
    leased_node_ids = [lease.node_id for lease in snapshot.leases]
    _require_unique(node_ids, "execution node")
    _require_unique(lease_ids, "execution lease")
    _require_unique(leased_node_ids, "leased execution node")
    known_nodes = set(node_ids)
    for lease in snapshot.leases:
        if not all(
            value.strip()
            for value in (lease.lease_id, lease.project_id, lease.work_id, lease.node_id)
        ):
            raise ProductCommandCenterScopeError(
                "execution snapshot contains an empty lease identity field"
            )
        if lease.node_id not in known_nodes:
            raise ProductCommandCenterScopeError(
                "execution snapshot lease references an unknown node"
            )
        if lease.expires_at <= lease.issued_at:
            raise ProductCommandCenterScopeError(
                "execution snapshot contains an invalid lease lifetime"
            )


def _scope_execution(
    project_id: str,
    snapshot: ExecutionRegistrySnapshot,
) -> ExecutionRegistrySnapshot:
    leases = tuple(lease for lease in snapshot.leases if lease.project_id == project_id)
    node_ids = {lease.node_id for lease in leases}
    nodes = tuple(node for node in snapshot.nodes if node.identity.node_id in node_ids)
    return ExecutionRegistrySnapshot(nodes, leases, snapshot.next_lease)


def _validate_deployment_snapshot(
    snapshot: DeploymentFabricSnapshot,
) -> tuple[tuple[str, str, str], ...]:
    intent_ids = [record.intent.intent_id for record in snapshot.records]
    staging_project_ids = [project_id for project_id, _sha in snapshot.healthy_staging]
    _require_unique(intent_ids, "deployment intent")
    _require_unique(staging_project_ids, "healthy-staging project")

    for record in snapshot.records:
        intent = record.intent
        if (
            intent.project_id != intent.environment.project_id
            or intent.project_id != intent.release.project_id
        ):
            raise ProductCommandCenterScopeError(
                "deployment record crosses ProductProject identity boundary"
            )
        if record.health is not None and (
            record.health.environment_id != intent.environment.environment_id
            or record.health.release_sha != intent.release.source_sha
        ):
            raise ProductCommandCenterScopeError(
                "deployment health evidence does not match deployment intent"
            )
        if record.rollback is not None and (
            record.rollback.environment_id != intent.environment.environment_id
            or record.rollback.failed_release_sha != intent.release.source_sha
        ):
            raise ProductCommandCenterScopeError(
                "deployment rollback evidence does not match failed release intent"
            )
        if record.state is DeploymentState.HEALTHY and (
            record.health is None or not record.health.healthy
        ):
            raise ProductCommandCenterScopeError(
                "healthy deployment state lacks matching healthy evidence"
            )
        if record.state is DeploymentState.ROLLED_BACK and (
            record.rollback is None or not record.rollback.succeeded
        ):
            raise ProductCommandCenterScopeError(
                "rolled-back deployment state lacks successful rollback evidence"
            )

    normalized = tuple(
        _normalize_current_release_entry(entry, snapshot.records)
        for entry in snapshot.current_releases
    )
    _require_unique(
        [(project_id, environment_id) for project_id, environment_id, _sha in normalized],
        "current-release project/environment",
    )
    return normalized


def _normalize_current_release_entry(
    entry: tuple[str, ...],
    records,
) -> tuple[str, str, str]:
    if len(entry) == 3:
        project_id, environment_id, release_sha = entry
    elif len(entry) == 2:
        environment_id, release_sha = entry
        projects = {
            record.intent.project_id
            for record in records
            if record.intent.environment.environment_id == environment_id
        }
        if len(projects) != 1:
            raise ProductCommandCenterScopeError(
                "legacy current-release environment identity is ambiguous across projects"
            )
        project_id = next(iter(projects))
    else:
        raise ProductCommandCenterScopeError(
            "current-release entry must use project/environment/release identity"
        )
    if not all(value.strip() for value in (project_id, environment_id, release_sha)):
        raise ProductCommandCenterScopeError(
            "current-release identity contains an empty field"
        )
    return project_id, environment_id, release_sha


def _scope_deployment(
    project_id: str,
    snapshot: DeploymentFabricSnapshot,
    normalized_current_releases: tuple[tuple[str, str, str], ...],
) -> DeploymentFabricSnapshot:
    records = tuple(
        record for record in snapshot.records if record.intent.project_id == project_id
    )
    healthy_staging = tuple(
        item for item in snapshot.healthy_staging if item[0] == project_id
    )
    current_releases = tuple(
        item for item in normalized_current_releases if item[0] == project_id
    )
    return DeploymentFabricSnapshot(records, healthy_staging, current_releases)


def _validate_deployment_execution_snapshot(
    snapshot: DeploymentExecutionSnapshot,
) -> None:
    operation_ids = [record.spec.operation_id for record in snapshot.records]
    _require_unique(operation_ids, "deployment operation")
    for record in snapshot.records:
        spec = record.spec
        if (
            spec.request.project_id != spec.intent.project_id
            or spec.intent.project_id != spec.intent.environment.project_id
            or spec.intent.project_id != spec.intent.release.project_id
        ):
            raise ProductCommandCenterScopeError(
                "deployment execution crosses ProductProject identity boundary"
            )
        if record.attempt < 0:
            raise ProductCommandCenterScopeError(
                "deployment execution contains an invalid attempt count"
            )


def _validate_deployment_wave_snapshot(snapshot: DeploymentWaveSnapshot) -> None:
    plan_ids = [record.plan.plan_id for record in snapshot.plans]
    _require_unique(plan_ids, "deployment wave plan")
    execution_id_list = [
        record.spec.operation_id for record in snapshot.execution.records
    ]
    _require_unique(execution_id_list, "deployment wave execution operation")
    execution_ids = set(execution_id_list)
    for record in snapshot.plans:
        service_ids = [service.service_id for service in record.services]
        operation_ids = [service.operation_id for service in record.services]
        _require_unique(service_ids, "deployment wave service")
        _require_unique(operation_ids, "deployment wave operation")
        expected = {
            service.execution.operation_id for service in record.plan.services
        }
        if set(operation_ids) != expected or not expected <= execution_ids:
            raise ProductCommandCenterScopeError(
                "deployment wave snapshot does not match execution evidence"
            )
        if any(
            service.execution.intent.project_id != record.plan.project_id
            for service in record.plan.services
        ):
            raise ProductCommandCenterScopeError(
                "deployment wave contains cross-project execution"
            )


def _validate_operations_scope(
    project_id: str,
    snapshot: ProductOperationsSnapshot,
) -> None:
    if snapshot.project_id != project_id:
        raise ProductCommandCenterScopeError(
            "operations snapshot belongs to a different ProductProject"
        )
    service_ids = [record.service.service_id for record in snapshot.services]
    request_ids = [
        record.request.request_id for record in snapshot.maintenance_records
    ]
    _require_unique(service_ids, "operations service")
    _require_unique(request_ids, "maintenance request")
    known_services = set(service_ids)
    for record in snapshot.services:
        service = record.service
        if service.project_id != project_id:
            raise ProductCommandCenterScopeError(
                "operations snapshot contains a cross-project service"
            )
    for record in snapshot.maintenance_records:
        if record.request.service_id not in known_services:
            raise ProductCommandCenterScopeError(
                "maintenance record references an unknown project service"
            )


def _validate_rolling_maintenance_snapshot(
    snapshot: RollingMaintenanceSnapshot,
) -> None:
    plan_ids = [plan.plan_id for plan in snapshot.plans]
    record_plan_ids = [plan_id for plan_id, _records in snapshot.node_records]
    _require_unique(plan_ids, "rolling maintenance plan")
    _require_unique(record_plan_ids, "rolling maintenance checkpoint plan")
    plans = {plan.plan_id: plan for plan in snapshot.plans}
    if set(record_plan_ids) != set(plans):
        raise ProductCommandCenterScopeError(
            "rolling maintenance checkpoint set does not match submitted plans"
        )
    for plan_id, records in snapshot.node_records:
        plan = plans[plan_id]
        node_ids = [record.node_id for record in records]
        _require_unique(node_ids, "rolling maintenance node")
        if tuple(node_ids) != plan.node_ids:
            raise ProductCommandCenterScopeError(
                "rolling maintenance node order/binding drifted from submitted plan"
            )
        for record in records:
            if record.pending_request is not None and (
                record.pending_request.plan_id != plan.plan_id
                or record.pending_request.project_id != plan.project_id
                or record.pending_request.fleet_plan_id != plan.fleet_plan_id
                or record.pending_request.node_id != record.node_id
            ):
                raise ProductCommandCenterScopeError(
                    "rolling maintenance pending request crosses plan identity"
                )


def _validate_credential_scope(
    project_id: str,
    declared_refs: tuple[str, ...],
    snapshot: CredentialBrokerSnapshot,
) -> None:
    if any(not ref.strip() for ref in declared_refs):
        raise ProductCommandCenterScopeError(
            "ProductProject contains an empty credential reference"
        )
    if len(declared_refs) != len(set(declared_refs)):
        raise ProductCommandCenterScopeError(
            "ProductProject contains duplicate credential references"
        )

    secret_refs = [secret.secret_ref for secret in snapshot.secrets]
    identity_refs = [identity.identity_ref for identity in snapshot.identities]
    audit_ids = [event.event_id for event in snapshot.audit_events]
    if len(secret_refs) != len(set(secret_refs)):
        raise ProductCommandCenterScopeError(
            "credential snapshot contains duplicate secret-reference identities"
        )
    if len(identity_refs) != len(set(identity_refs)):
        raise ProductCommandCenterScopeError(
            "credential snapshot contains duplicate identity-reference identities"
        )
    if len(audit_ids) != len(set(audit_ids)):
        raise ProductCommandCenterScopeError(
            "credential snapshot contains duplicate audit-event identities"
        )

    secrets = {secret.secret_ref: secret for secret in snapshot.secrets}
    target_refs = {
        secret.secret_ref for secret in snapshot.secrets if secret.project_id == project_id
    }
    for declared_ref in declared_refs:
        secret = secrets.get(declared_ref)
        if secret is not None and secret.project_id != project_id:
            raise ProductCommandCenterScopeError(
                "ProductProject credential reference resolves to another project"
            )

    for identity in snapshot.identities:
        touches_target = identity.project_id == project_id or any(
            ref in target_refs for ref in identity.secret_refs
        )
        if not touches_target:
            continue
        if identity.project_id != project_id:
            raise ProductCommandCenterScopeError(
                "credential identity crosses ProductProject boundary"
            )
        for secret_ref in identity.secret_refs:
            secret = secrets.get(secret_ref)
            if secret is None or secret.project_id != project_id:
                raise ProductCommandCenterScopeError(
                    "credential identity binds a secret outside ProductProject scope"
                )
            if secret.provider != identity.provider:
                raise ProductCommandCenterScopeError(
                    "credential identity provider does not match bound secret provider"
                )

    for event in snapshot.audit_events:
        touches_target = event.project_id == project_id or event.secret_ref in target_refs
        if not touches_target:
            continue
        secret = secrets.get(event.secret_ref)
        if (
            event.project_id != project_id
            or secret is None
            or secret.project_id != project_id
        ):
            raise ProductCommandCenterScopeError(
                "credential audit event crosses ProductProject boundary"
            )


def _require_unique(values: Iterable[object], label: str) -> None:
    values = list(values)
    if len(values) != len(set(values)):
        raise ProductCommandCenterScopeError(f"snapshot contains duplicate {label} identities")


def _require_unique_status_identity(statuses) -> None:
    seen: set[tuple[ProductStatusKind, str]] = set()
    for item in statuses:
        identity = (item.kind, item.item_id)
        if identity in seen:
            raise ProductCommandCenterScopeError(
                f"duplicate ProductProject status identity: {item.kind.value}/{item.item_id}"
            )
        seen.add(identity)
