from __future__ import annotations

from nika_core.product_command.contracts import ProductProjectDetail, ProductStatusKind
from nika_core.product_command.coordinator_adapter import coordinator_status_entries
from nika_core.product_command.deployment_adapter import (
    deployment_status_entries,
    execution_status_entries,
)
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_coordinator import CoordinatorSnapshot
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    ExecutionRegistrySnapshot,
)


class ProductCommandCenterScopeError(ValueError):
    """Raised when a Product Factory snapshot cannot be safely scoped to one project."""


class ProductCommandCenter:
    """Compose PF1/PF2/PF3 presentation without leaking cross-project state."""

    def __init__(self, projects: ProductProjectCommandService) -> None:
        self._projects = projects

    def inspect_project(
        self,
        project_id: str,
        *,
        coordinator: CoordinatorSnapshot | None = None,
        execution: ExecutionRegistrySnapshot | None = None,
        deployment: DeploymentFabricSnapshot | None = None,
    ) -> ProductProjectDetail:
        detail = self._projects.inspect_project(project_id)
        statuses = list(detail.statuses)

        if coordinator is not None:
            _validate_coordinator_scope(project_id, coordinator)
            statuses.extend(coordinator_status_entries(coordinator))
        if execution is not None:
            statuses.extend(execution_status_entries(_scope_execution(project_id, execution)))
        if deployment is not None:
            statuses.extend(
                deployment_status_entries(_scope_deployment(project_id, deployment))
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
    if any(record.request.project_id != project_id for record in snapshot.records):
        raise ProductCommandCenterScopeError(
            "coordinator snapshot contains cross-project work records"
        )


def _scope_execution(
    project_id: str,
    snapshot: ExecutionRegistrySnapshot,
) -> ExecutionRegistrySnapshot:
    leases = tuple(lease for lease in snapshot.leases if lease.project_id == project_id)
    node_ids = {lease.node_id for lease in leases}
    nodes = tuple(node for node in snapshot.nodes if node.identity.node_id in node_ids)
    return ExecutionRegistrySnapshot(nodes, leases, snapshot.next_lease)


def _scope_deployment(
    project_id: str,
    snapshot: DeploymentFabricSnapshot,
) -> DeploymentFabricSnapshot:
    records = tuple(
        record for record in snapshot.records if record.intent.project_id == project_id
    )
    healthy_staging = tuple(
        item for item in snapshot.healthy_staging if item[0] == project_id
    )
    environment_ids = {record.intent.environment.environment_id for record in records}
    current_releases = tuple(
        item for item in snapshot.current_releases if item[0] in environment_ids
    )
    return DeploymentFabricSnapshot(records, healthy_staging, current_releases)


def _require_unique_status_identity(statuses) -> None:
    seen: set[tuple[ProductStatusKind, str]] = set()
    for item in statuses:
        identity = (item.kind, item.item_id)
        if identity in seen:
            raise ProductCommandCenterScopeError(
                f"duplicate ProductProject status identity: {item.kind.value}/{item.item_id}"
            )
        seen.add(identity)
