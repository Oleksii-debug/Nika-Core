from __future__ import annotations

from nika_core.product_factory_fleet_maintenance import (
    NodeMaintenanceAction,
    NodeMaintenanceState,
    RollingMaintenanceSnapshot,
)
from nika_core.product_factory_operations import ProductOperationsSnapshot
from nika_core.product_factory_operations_contracts import ServiceHealth


class ProductFactorySnapshotIntegrityError(ValueError):
    """Raised when PF3 durable state is not safe to project as product truth."""


def validate_operations_projection(
    project_id: str,
    snapshot: ProductOperationsSnapshot,
) -> None:
    if snapshot.project_id != project_id:
        raise ProductFactorySnapshotIntegrityError(
            "operations snapshot belongs to a different ProductProject"
        )

    service_ids = [record.service.service_id for record in snapshot.services]
    if len(service_ids) != len(set(service_ids)):
        raise ProductFactorySnapshotIntegrityError(
            "operations snapshot contains duplicate service identities"
        )
    services = {record.service.service_id: record for record in snapshot.services}
    revoked = set(snapshot.revoked_credentials)
    unavailable_nodes = set(snapshot.unavailable_nodes)
    if len(revoked) != len(snapshot.revoked_credentials):
        raise ProductFactorySnapshotIntegrityError(
            "operations snapshot contains duplicate revoked credentials"
        )
    if len(unavailable_nodes) != len(snapshot.unavailable_nodes):
        raise ProductFactorySnapshotIntegrityError(
            "operations snapshot contains duplicate unavailable nodes"
        )

    for record in snapshot.services:
        service = record.service
        if service.project_id != project_id:
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot contains a cross-project service"
            )
        for dependency_id in service.dependencies:
            dependency = services.get(dependency_id)
            if dependency is None:
                raise ProductFactorySnapshotIntegrityError(
                    "operations snapshot service dependency is missing"
                )
            if dependency.service.wave >= service.wave:
                raise ProductFactorySnapshotIntegrityError(
                    "operations dependency is not bound to an earlier wave"
                )

        expected_node_loss = {
            replica.replica_id
            for replica in service.replicas
            if replica.node_id in unavailable_nodes
        }
        if len(record.node_loss) != len(set(record.node_loss)):
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot contains duplicate node-loss replicas"
            )
        if set(record.node_loss) != expected_node_loss:
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot node-loss evidence disagrees with unavailable nodes"
            )

        blocked = set(record.blocked_credentials)
        service_credentials = set(service.credential_refs)
        expected_blocked = service_credentials & revoked
        if len(blocked) != len(record.blocked_credentials):
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot contains duplicate blocked credentials"
            )
        if not blocked <= service_credentials:
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot blocks a credential not declared by the service"
            )
        if not blocked <= revoked:
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot credential blocker lacks revocation state"
            )
        if blocked != expected_blocked:
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot omits revoked service credential blocker"
            )
        if bool(blocked) != (record.health is ServiceHealth.BLOCKED):
            raise ProductFactorySnapshotIntegrityError(
                "operations snapshot credential blocker disagrees with service health"
            )

        observation = record.observation
        if observation is not None:
            known_replicas = {replica.replica_id for replica in service.replicas}
            observed_replicas = set(observation.healthy_replica_ids) | set(
                observation.failed_replica_ids
            )
            if (
                observation.service_id != service.service_id
                or observation.release_sha != service.release_sha
                or not observed_replicas <= known_replicas
            ):
                raise ProductFactorySnapshotIntegrityError(
                    "operations observation does not match service identity/release/replicas"
                )

        rollback = record.rollback
        if rollback is not None:
            if (
                rollback.service_id != service.service_id
                or rollback.failed_release_sha != service.release_sha
            ):
                raise ProductFactorySnapshotIntegrityError(
                    "operations rollback evidence disagrees with service state"
                )
            if observation is None:
                raise ProductFactorySnapshotIntegrityError(
                    "operations rollback evidence lacks prior service observation"
                )

        expected_health = _expected_health_states(record)
        if record.health not in expected_health:
            raise ProductFactorySnapshotIntegrityError(
                "operations service health disagrees with credential/observation/rollback state"
            )

    request_ids = [record.request.request_id for record in snapshot.maintenance_records]
    if len(request_ids) != len(set(request_ids)):
        raise ProductFactorySnapshotIntegrityError(
            "operations snapshot contains duplicate maintenance identities"
        )
    for maintenance in snapshot.maintenance_records:
        if maintenance.request.service_id not in services:
            raise ProductFactorySnapshotIntegrityError(
                "maintenance record references an unknown project service"
            )
        if maintenance.request.approval_ref is None:
            raise ProductFactorySnapshotIntegrityError(
                "durable maintenance record lacks explicit approval identity"
            )


def _expected_health_states(record) -> set[ServiceHealth]:
    if record.blocked_credentials:
        return {ServiceHealth.BLOCKED}
    observation = record.observation
    if observation is None:
        return {ServiceHealth.PENDING}

    service = record.service
    observed_health = _health_from_observation(
        service.min_healthy_replicas,
        len(service.replicas),
        observation.healthy_replica_ids,
        observation.failed_replica_ids,
        record.node_loss,
    )
    expected = {observed_health}
    if record.rollback is not None:
        expected.add(
            ServiceHealth.ROLLED_BACK
            if record.rollback.succeeded
            else ServiceHealth.FAILED
        )
    return expected


def _health_from_observation(
    min_healthy_replicas: int,
    replica_count: int,
    healthy_replica_ids: tuple[str, ...],
    failed_replica_ids: tuple[str, ...],
    node_loss: tuple[str, ...],
) -> ServiceHealth:
    loss = set(node_loss)
    healthy = set(healthy_replica_ids) - loss
    failed = set(failed_replica_ids) | loss
    if len(healthy) >= min_healthy_replicas:
        if failed or len(healthy) < replica_count:
            return ServiceHealth.DEGRADED
        return ServiceHealth.HEALTHY
    return ServiceHealth.DEGRADED if healthy else ServiceHealth.ROLLBACK_REQUIRED


def validate_rolling_maintenance_projection(
    project_id: str,
    snapshot: RollingMaintenanceSnapshot,
) -> None:
    plan_ids = [plan.plan_id for plan in snapshot.plans]
    checkpoint_plan_ids = [plan_id for plan_id, _records in snapshot.node_records]
    if len(plan_ids) != len(set(plan_ids)):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance snapshot contains duplicate plans"
        )
    if len(checkpoint_plan_ids) != len(set(checkpoint_plan_ids)):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance snapshot contains duplicate checkpoint plans"
        )
    if set(plan_ids) != set(checkpoint_plan_ids):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance checkpoint set does not match submitted plans"
        )

    plans = {plan.plan_id: plan for plan in snapshot.plans}
    if any(plan.project_id != project_id for plan in snapshot.plans):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance snapshot belongs to a different ProductProject"
        )

    for plan_id, records in snapshot.node_records:
        plan = plans[plan_id]
        node_ids = tuple(record.node_id for record in records)
        if node_ids != plan.node_ids:
            raise ProductFactorySnapshotIntegrityError(
                "rolling maintenance node order/binding drifted from submitted plan"
            )
        if len(node_ids) != len(set(node_ids)):
            raise ProductFactorySnapshotIntegrityError(
                "rolling maintenance snapshot contains duplicate nodes"
            )
        for record in records:
            _validate_maintenance_record(plan, record)


def _validate_maintenance_record(plan, record) -> None:
    completed = record.completed_actions
    if completed != _ACTION_ORDER[: len(completed)] or len(completed) > len(_ACTION_ORDER):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance completed-action history is invalid"
        )

    exact_lengths = {
        NodeMaintenanceState.PENDING: 0,
        NodeMaintenanceState.CORDONED: 0,
        NodeMaintenanceState.BLOCKED_ACTIVE_LEASE: 0,
        NodeMaintenanceState.DRAINED: 1,
        NodeMaintenanceState.RESTARTED: 2,
        NodeMaintenanceState.VERIFIED: 3,
        NodeMaintenanceState.SUCCEEDED: 4,
    }
    exact_length = exact_lengths.get(record.state)
    if exact_length is not None and len(completed) != exact_length:
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance state disagrees with completed actions"
        )
    if record.state in {
        NodeMaintenanceState.BLOCKED_QUORUM,
        NodeMaintenanceState.BLOCKED_CREDENTIAL,
        NodeMaintenanceState.RECONCILE_REQUIRED,
        NodeMaintenanceState.FAILED,
    } and len(completed) >= len(_ACTION_ORDER):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance non-success state carries completed plan"
        )

    pending = record.pending_request
    if record.state is NodeMaintenanceState.RECONCILE_REQUIRED and pending is None:
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance reconcile state lacks durable pending request"
        )
    if pending is None:
        return
    if record.state is not NodeMaintenanceState.RECONCILE_REQUIRED:
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance pending request exists outside reconcile state"
        )
    if len(completed) >= len(_ACTION_ORDER):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance pending request exists after completed plan"
        )
    expected_action = _ACTION_ORDER[len(completed)]
    expected_request_id = f"{plan.plan_id}:{record.node_id}:{expected_action.value}"
    if (
        pending.request_id != expected_request_id
        or pending.plan_id != plan.plan_id
        or pending.project_id != plan.project_id
        or pending.fleet_plan_id != plan.fleet_plan_id
        or pending.node_id != record.node_id
        or pending.action is not expected_action
        or pending.bindings != record.bindings
        or pending.reason != plan.reason
        or pending.approval_ref != plan.approval_ref
    ):
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance pending request disagrees with durable plan/approval/action"
        )
    if pending.evidence_refs[: len(plan.evidence_refs)] != plan.evidence_refs:
        raise ProductFactorySnapshotIntegrityError(
            "rolling maintenance pending request lost submitted plan evidence"
        )


_ACTION_ORDER = (
    NodeMaintenanceAction.DRAIN,
    NodeMaintenanceAction.RESTART,
    NodeMaintenanceAction.VERIFY,
    NodeMaintenanceAction.RESUME,
)
