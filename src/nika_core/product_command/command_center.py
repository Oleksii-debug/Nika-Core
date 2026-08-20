from __future__ import annotations

from nika_core.product_command.contracts import ProductProjectDetail, ProductStatusKind
from nika_core.product_command.coordinator_adapter import coordinator_status_entries
from nika_core.product_command.credential_adapter import credential_status_entries
from nika_core.product_command.deployment_adapter import (
    deployment_status_entries,
    execution_status_entries,
)
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_coordinator import CoordinatorSnapshot
from nika_core.product_factory_credentials import CredentialBrokerSnapshot
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
        credentials: CredentialBrokerSnapshot | None = None,
    ) -> ProductProjectDetail:
        detail, credential_refs = self._projects.inspect_project_context(project_id)
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
