from __future__ import annotations

from nika_core.product_command.contracts import ProductProjectDetail, ProductStatusKind
from nika_core.product_command.coordinator_adapter import coordinator_status_entries
from nika_core.product_command.credential_adapter import credential_status_entries
from nika_core.product_command.deployment_adapter import (
    deployment_status_entries,
    execution_status_entries,
)
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_coordinator import CoordinatorSnapshot, WorkState
from nika_core.product_factory_credentials import CredentialBrokerSnapshot
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentState,
    ExecutionRegistrySnapshot,
)


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
            _validate_deployment_snapshot(deployment)
            statuses.extend(
                deployment_status_entries(_scope_deployment(project_id, deployment))
            )
        if credentials is not None:
            _validate_credential_scope(project_id, credential_refs, credentials)
            statuses.extend(
                credential_status_entries(project_id, credential_refs, credentials)
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
    if len(component_ids) != len(set(component_ids)):
        raise ProductCommandCenterScopeError(
            "coordinator snapshot contains duplicate component identities"
        )
    if len(work_ids) != len(set(work_ids)):
        raise ProductCommandCenterScopeError(
            "coordinator snapshot contains duplicate work identities"
        )
    for record in snapshot.records:
        request = record.request
        if request.project_id != project_id:
            raise ProductCommandCenterScopeError(
                "coordinator snapshot contains cross-project work records"
            )
        result = record.result
        if result is not None:
            if (
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
        if record.state is WorkState.ACCEPTED:
            if result is None or record.review is None or not record.review.accepted:
                raise ProductCommandCenterScopeError(
                    "accepted coordinator record lacks accepted independent review evidence"
                )


def _validate_execution_snapshot(snapshot: ExecutionRegistrySnapshot) -> None:
    node_ids = [node.identity.node_id for node in snapshot.nodes]
    lease_ids = [lease.lease_id for lease in snapshot.leases]
    leased_node_ids = [lease.node_id for lease in snapshot.leases]
    if len(node_ids) != len(set(node_ids)):
        raise ProductCommandCenterScopeError(
            "execution snapshot contains duplicate node identities"
        )
    if len(lease_ids) != len(set(lease_ids)):
        raise ProductCommandCenterScopeError(
            "execution snapshot contains duplicate lease identities"
        )
    if len(leased_node_ids) != len(set(leased_node_ids)):
        raise ProductCommandCenterScopeError(
            "execution snapshot assigns one node to multiple active leases"
        )
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


def _validate_deployment_snapshot(snapshot: DeploymentFabricSnapshot) -> None:
    intent_ids = [record.intent.intent_id for record in snapshot.records]
    staging_project_ids = [project_id for project_id, _sha in snapshot.healthy_staging]
    current_environment_ids = [environment_id for environment_id, _sha in snapshot.current_releases]
    if len(intent_ids) != len(set(intent_ids)):
        raise ProductCommandCenterScopeError(
            "deployment snapshot contains duplicate intent identities"
        )
    if len(staging_project_ids) != len(set(staging_project_ids)):
        raise ProductCommandCenterScopeError(
            "deployment snapshot contains duplicate healthy-staging project identities"
        )
    if len(current_environment_ids) != len(set(current_environment_ids)):
        raise ProductCommandCenterScopeError(
            "deployment snapshot contains duplicate current-release environment identities"
        )

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


def _require_unique_status_identity(statuses) -> None:
    seen: set[tuple[ProductStatusKind, str]] = set()
    for item in statuses:
        identity = (item.kind, item.item_id)
        if identity in seen:
            raise ProductCommandCenterScopeError(
                f"duplicate ProductProject status identity: {item.kind.value}/{item.item_id}"
            )
        seen.add(identity)
