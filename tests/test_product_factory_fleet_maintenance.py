from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_factory_deployment_fleet import (
    DeploymentFleetPlan,
    DeploymentFleetRecord,
    FleetState,
    ReplicaFleetRecord,
    ServiceFleetRecord,
    ServiceFleetSpec,
)
from nika_core.product_factory_fleet_maintenance import (
    FleetMaintenanceError,
    NodeMaintenanceAction,
    NodeMaintenanceResult,
    NodeMaintenanceState,
    RollingFleetMaintenanceCoordinator,
    RollingMaintenancePlan,
    RollingMaintenanceState,
)
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


class _SimulatedProcessLoss(BaseException):
    """Bypass normal Exception handling like abrupt process loss."""


class FakeFleet:
    def __init__(self, record: DeploymentFleetRecord) -> None:
        self.record = record

    def get(self, plan_id: str) -> DeploymentFleetRecord:
        if plan_id != self.record.plan.plan_id:
            raise AssertionError("unknown fake fleet plan")
        return self.record


class FakeNodeMaintenance:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.modes: dict[str, str] = {}

    def apply(self, request):
        self.calls.append(("apply", request.request_id))
        mode = self.modes.get(request.request_id, "success")
        if mode == "crash-after-effect":
            self.modes[request.request_id] = "applied-after-crash"
            raise _SimulatedProcessLoss()
        if mode == "uncertain":
            return NodeMaintenanceResult(False, True, ("provider:uncertain",))
        if mode == "rejected":
            return NodeMaintenanceResult(False, False, ("provider:rejected",))
        verified = ()
        if request.action is NodeMaintenanceAction.VERIFY:
            verified = tuple(
                replica_id
                for binding in request.bindings
                for replica_id in binding.replica_ids
            )
            if mode == "incomplete-verify":
                verified = verified[:-1]
        return NodeMaintenanceResult(
            True,
            False,
            (f"provider:{request.action.value}:ok",),
            verified,
        )

    def inspect(self, request):
        self.calls.append(("inspect", request.request_id))
        mode = self.modes.get(request.request_id, "success")
        if mode == "still-uncertain":
            return NodeMaintenanceResult(False, True, ("provider:still-uncertain",))
        self.modes[request.request_id] = "success"
        verified = ()
        if request.action is NodeMaintenanceAction.VERIFY:
            verified = tuple(
                replica_id
                for binding in request.bindings
                for replica_id in binding.replica_ids
            )
        return NodeMaintenanceResult(
            True,
            False,
            (f"provider:inspect:{request.action.value}:ok",),
            verified,
        )


def _sha(seed: int) -> str:
    return f"{seed:040x}"[-40:]


def _digest(seed: int) -> str:
    return f"{seed + 50_000:064x}"[-64:]


def _execution_spec(
    service_id: str,
    replica: int,
    *,
    project_id: str,
    environment_id: str,
    release_seed: int,
) -> DeploymentExecutionSpec:
    release = ReleaseRef(project_id, "1.0.0", _sha(release_seed), _digest(release_seed))
    environment = EnvironmentIdentity(
        environment_id,
        project_id,
        EnvironmentTier.PRODUCTION,
        "provider:authorized-staging-adapter",
    )
    suffix = f"{service_id}-{replica}"
    return DeploymentExecutionSpec(
        f"op-{suffix}",
        ExecutionRequest(
            project_id,
            f"work-{suffix}",
            Platform.LINUX,
            frozenset({"deploy"}),
            frozenset({"ansible"}),
            ResourceEnvelope(1, 256, 512),
        ),
        DeploymentIntent(
            f"intent-{suffix}",
            project_id,
            environment,
            release,
        ),
        "cred:deploy",
        "provider",
        "deploy",
    )


def _fixture(
    tmp_path,
    *,
    service_count: int = 2,
    replica_count: int = 3,
    minimum: int = 2,
    node_count: int = 3,
):
    project_id = "project-social"
    environment_id = "prod-eu"
    nodes = ExecutionNodeRegistry()
    for index in range(node_count):
        nodes.register(
            ExecutionNode(
                NodeIdentity(
                    f"node-{index}",
                    Platform.LINUX,
                    "x86_64",
                    f"instance-{index}",
                ),
                NodeCapabilities(
                    frozenset({"deploy"}),
                    frozenset({"ansible"}),
                ),
                ResourceEnvelope(8, 16_384, 100_000),
            )
        )

    operations = ProductOperationsCoordinator(project_id)
    service_specs: list[ServiceFleetSpec] = []
    service_records: list[ServiceFleetRecord] = []
    for service_index in range(service_count):
        service_id = f"service-{service_index:03d}"
        release_seed = service_index + 1
        replicas = tuple(
            _execution_spec(
                service_id,
                replica,
                project_id=project_id,
                environment_id=environment_id,
                release_seed=release_seed,
            )
            for replica in range(replica_count)
        )
        spec = ServiceFleetSpec(service_id, 0, replicas, minimum)
        service_specs.append(spec)
        fleet_replicas = tuple(
            ReplicaFleetRecord(
                replica.operation_id,
                replica.request.work_id,
                OperationState.SUCCEEDED,
                f"node-{index % node_count}",
                1,
                ("provider:healthy",),
            )
            for index, replica in enumerate(replicas)
        )
        service_records.append(
            ServiceFleetRecord(
                service_id,
                spec.release.source_sha,
                spec.release.artifact_digest,
                environment_id,
                FleetState.HEALTHY,
                replica_count,
                minimum,
                fleet_replicas,
            )
        )

        ops_replicas = tuple(
            ServiceReplica(f"{service_id}-replica-{index}", f"node-{index % node_count}")
            for index in range(replica_count)
        )
        operations.register(
            DeployableService(
                service_id,
                project_id,
                environment_id,
                spec.release.source_sha,
                0,
                ops_replicas,
                minimum,
                credential_refs=("cred:deploy",),
            )
        )
        operations.record_observation(
            ServiceObservation(
                service_id,
                spec.release.source_sha,
                tuple(replica.replica_id for replica in ops_replicas),
                (),
                (f"health:{service_id}",),
                datetime(2026, 8, 21, 10, service_index % 60, tzinfo=UTC),
            )
        )

    plan = DeploymentFleetPlan("fleet-production", project_id, tuple(service_specs))
    fleet = FakeFleet(
        DeploymentFleetRecord(
            plan,
            FleetState.HEALTHY,
            tuple(service_records),
        )
    )
    port = FakeNodeMaintenance()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = IdempotencyLedger(store)
    coordinator = RollingFleetMaintenanceCoordinator(
        fleet,
        operations,
        nodes,
        port,
        ledger,
    )
    return coordinator, fleet, operations, nodes, port


def _plan(*node_ids: str) -> RollingMaintenancePlan:
    return RollingMaintenancePlan(
        "maintenance-001",
        "project-social",
        "fleet-production",
        tuple(node_ids),
        "approval:ops-window-42",
        "rolling node maintenance",
        ("change:maintenance-001",),
    )


def test_rolling_node_maintenance_cordons_drains_verifies_and_resumes(tmp_path) -> None:
    coordinator, _, operations, nodes, port = _fixture(tmp_path, service_count=2)
    coordinator.submit(_plan("node-0"))

    drained = coordinator.advance("maintenance-001")
    node = drained.nodes[0]
    assert node.state is NodeMaintenanceState.DRAINED
    assert node.cordoned
    assert all(
        not item.enabled
        for item in nodes.snapshot().nodes
        if item.identity.node_id == "node-0"
    )
    assert operations.health_summary().degraded == ("service-000", "service-001")

    coordinator.advance("maintenance-001")
    coordinator.advance("maintenance-001")
    finished = coordinator.advance("maintenance-001")

    assert finished.state is RollingMaintenanceState.SUCCEEDED
    assert finished.nodes[0].state is NodeMaintenanceState.SUCCEEDED
    assert not finished.nodes[0].cordoned
    assert operations.health_summary().healthy == ("service-000", "service-001")
    assert all(
        item.enabled
        for item in nodes.snapshot().nodes
        if item.identity.node_id == "node-0"
    )
    assert len(port.calls) == 4


def test_active_execution_lease_cordons_node_then_blocks_drain_without_side_effect(tmp_path) -> None:
    coordinator, _, _, nodes, port = _fixture(tmp_path, service_count=1)
    lease = nodes.acquire(
        ExecutionRequest(
            "project-social",
            "work-in-flight",
            Platform.LINUX,
            frozenset({"deploy"}),
            frozenset({"ansible"}),
            ResourceEnvelope(1, 128, 128),
        ),
        now=datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
    )
    assert lease.node_id == "node-0"
    coordinator.submit(_plan("node-0"))

    blocked = coordinator.advance("maintenance-001")
    assert blocked.nodes[0].state is NodeMaintenanceState.BLOCKED_ACTIVE_LEASE
    assert blocked.nodes[0].cordoned
    assert port.calls == []

    nodes.release(lease.lease_id)
    drained = coordinator.advance("maintenance-001")
    assert drained.nodes[0].state is NodeMaintenanceState.DRAINED


def test_quorum_guard_blocks_destructive_drain_before_cordon(tmp_path) -> None:
    coordinator, _, operations, nodes, port = _fixture(tmp_path, service_count=1)
    operations.record_node_availability("node-1", available=False)
    coordinator.submit(_plan("node-0"))

    blocked = coordinator.advance("maintenance-001")
    assert blocked.state is RollingMaintenanceState.BLOCKED
    assert blocked.nodes[0].state is NodeMaintenanceState.BLOCKED_QUORUM
    assert not blocked.nodes[0].cordoned
    assert port.calls == []
    assert next(
        item for item in nodes.snapshot().nodes if item.identity.node_id == "node-0"
    ).enabled


def test_credential_revocation_mid_operation_blocks_next_external_action(tmp_path) -> None:
    coordinator, _, operations, _, port = _fixture(tmp_path, service_count=1)
    coordinator.submit(_plan("node-0"))
    coordinator.advance("maintenance-001")
    calls_after_drain = len(port.calls)

    assert operations.revoke_credential("cred:deploy") == ("service-000",)
    blocked = coordinator.advance("maintenance-001")
    assert blocked.nodes[0].state is NodeMaintenanceState.BLOCKED_CREDENTIAL
    assert len(port.calls) == calls_after_drain

    operations.restore_credential("cred:deploy")
    restarted = coordinator.advance("maintenance-001")
    assert restarted.nodes[0].state is NodeMaintenanceState.RESTARTED


def test_uncertain_drain_requires_inspect_and_never_blind_replays_apply(tmp_path) -> None:
    coordinator, _, operations, _, port = _fixture(tmp_path, service_count=1)
    request_id = "maintenance-001:node-0:drain"
    port.modes[request_id] = "uncertain"
    coordinator.submit(_plan("node-0"))

    uncertain = coordinator.advance("maintenance-001")
    assert uncertain.state is RollingMaintenanceState.RECONCILE_REQUIRED
    assert uncertain.nodes[0].state is NodeMaintenanceState.RECONCILE_REQUIRED
    assert operations.health_summary().degraded == ("service-000",)

    reconciled = coordinator.advance("maintenance-001")
    assert reconciled.nodes[0].state is NodeMaintenanceState.DRAINED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1
    assert coordinator.idempotency is not None
    assert coordinator.idempotency.require(
        f"fleet-maintenance:{request_id}"
    ).status is IdempotencyStatus.COMPLETED


def test_crash_after_provider_effect_restarts_via_inspect_without_duplicate_apply(tmp_path) -> None:
    coordinator, fleet, operations, nodes, port = _fixture(tmp_path, service_count=1)
    request_id = "maintenance-001:node-0:drain"
    coordinator.submit(_plan("node-0"))
    old_maintenance = coordinator.snapshot()
    old_nodes = nodes.snapshot()
    port.modes[request_id] = "crash-after-effect"

    with pytest.raises(_SimulatedProcessLoss):
        coordinator.advance("maintenance-001")

    assert port.calls.count(("apply", request_id)) == 1
    assert coordinator.idempotency is not None
    assert coordinator.idempotency.require(
        f"fleet-maintenance:{request_id}"
    ).status is IdempotencyStatus.PENDING

    nodes.restore(old_nodes)
    restarted = RollingFleetMaintenanceCoordinator(
        fleet,
        operations,
        nodes,
        port,
        coordinator.idempotency,
    )
    restarted.restore(old_maintenance)

    reconcile_required = restarted.advance("maintenance-001")
    assert reconcile_required.state is RollingMaintenanceState.RECONCILE_REQUIRED
    assert port.calls.count(("apply", request_id)) == 1

    drained = restarted.advance("maintenance-001")
    assert drained.nodes[0].state is NodeMaintenanceState.DRAINED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1
    assert coordinator.idempotency.require(
        f"fleet-maintenance:{request_id}"
    ).status is IdempotencyStatus.COMPLETED


def test_verify_fails_closed_when_provider_does_not_prove_exact_drained_replicas(tmp_path) -> None:
    coordinator, _, _, _, port = _fixture(tmp_path, service_count=1)
    coordinator.submit(_plan("node-0"))
    coordinator.advance("maintenance-001")
    coordinator.advance("maintenance-001")
    port.modes["maintenance-001:node-0:verify"] = "incomplete-verify"

    with pytest.raises(FleetMaintenanceError, match="exactly the drained replicas"):
        coordinator.advance("maintenance-001")


def test_snapshot_restore_requires_execution_node_cordon_parity(tmp_path) -> None:
    coordinator, fleet, operations, nodes, port = _fixture(tmp_path, service_count=1)
    coordinator.submit(_plan("node-0"))
    coordinator.advance("maintenance-001")
    snapshot = coordinator.snapshot()

    restored = RollingFleetMaintenanceCoordinator(
        fleet, operations, nodes, port, coordinator.idempotency
    )
    restored.restore(snapshot)
    assert restored.get("maintenance-001").nodes[0].state is NodeMaintenanceState.DRAINED

    node = next(
        item for item in nodes.snapshot().nodes if item.identity.node_id == "node-0"
    )
    nodes.register(replace(node, enabled=True))
    corrupt = RollingFleetMaintenanceCoordinator(
        fleet, operations, nodes, port, coordinator.idempotency
    )
    with pytest.raises(FleetMaintenanceError, match="lost its cordon"):
        corrupt.restore(snapshot)


def test_release_or_topology_drift_is_rejected_before_next_side_effect(tmp_path) -> None:
    coordinator, fleet, _, _, port = _fixture(tmp_path, service_count=1)
    coordinator.submit(_plan("node-0"))
    coordinator.advance("maintenance-001")
    calls = len(port.calls)

    service = fleet.record.services[0]
    fleet.record = replace(
        fleet.record,
        services=(
            replace(
                service,
                release_sha=_sha(999),
            ),
        ),
    )
    with pytest.raises(FleetMaintenanceError, match="provenance disagree"):
        coordinator.advance("maintenance-001")
    assert len(port.calls) == calls


def test_scale_sixty_services_180_replicas_restart_between_rolling_nodes(tmp_path) -> None:
    coordinator, fleet, operations, nodes, port = _fixture(tmp_path, service_count=60)
    coordinator.submit(_plan("node-0", "node-1", "node-2"))

    for _ in range(4):
        coordinator.advance("maintenance-001")
    first = coordinator.get("maintenance-001")
    assert first.nodes[0].state is NodeMaintenanceState.SUCCEEDED
    assert first.nodes[1].state is NodeMaintenanceState.PENDING
    assert operations.health_summary().healthy == tuple(
        f"service-{index:03d}" for index in range(60)
    )

    snapshot = coordinator.snapshot()
    restarted = RollingFleetMaintenanceCoordinator(
        fleet, operations, nodes, port, coordinator.idempotency
    )
    restarted.restore(snapshot)
    for _ in range(8):
        restarted.advance("maintenance-001")

    final = restarted.get("maintenance-001")
    assert final.state is RollingMaintenanceState.SUCCEEDED
    assert all(node.state is NodeMaintenanceState.SUCCEEDED for node in final.nodes)
    assert len(port.calls) == 12
    assert operations.health_summary().healthy == tuple(
        f"service-{index:03d}" for index in range(60)
    )
