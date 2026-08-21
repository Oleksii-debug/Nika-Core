from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_deployment import (
    DeploymentIntent,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionRequest,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionRecord,
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_factory_deployment_fleet import (
    DeploymentFleetCoordinator,
    DeploymentFleetError,
    DeploymentFleetPlan,
    DeploymentFleetSnapshot,
    FleetState,
    ServiceFleetSpec,
)


class FakeExecution:
    def __init__(self, node_count: int = 256) -> None:
        self.records: dict[str, DeploymentExecutionRecord] = {}
        self.available_nodes = [f"node-{index:03d}" for index in range(node_count)]
        self.active: dict[str, str] = {}
        self.complete_modes: dict[str, str] = {}
        self.prepare_blocks: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def submit(self, spec: DeploymentExecutionSpec) -> DeploymentExecutionRecord:
        existing = self.records.get(spec.operation_id)
        if existing is not None and existing.spec != spec:
            raise AssertionError("fake operation conflict")
        if existing is not None:
            return existing
        record = DeploymentExecutionRecord(spec, OperationState.PENDING)
        self.records[spec.operation_id] = record
        self.calls.append(("submit", spec.operation_id))
        return record

    def prepare(self, operation_id: str) -> DeploymentExecutionRecord:
        self.calls.append(("prepare", operation_id))
        record = self.records[operation_id]
        if operation_id in self.prepare_blocks or not self.available_nodes:
            return self._save(replace(record, state=OperationState.WAITING_FOR_NODE))
        node_id = self.available_nodes.pop(0)
        self.active[operation_id] = node_id
        return self._save(
            replace(
                record,
                state=OperationState.PREPARED,
                node_id=node_id,
                attempt=record.attempt + 1,
                evidence_refs=record.evidence_refs + (f"execution-node:{node_id}",),
            )
        )

    def complete(self, operation_id: str) -> DeploymentExecutionRecord:
        self.calls.append(("complete", operation_id))
        record = self.records[operation_id]
        node_id = self.active.pop(operation_id, None)
        if node_id is not None:
            self.available_nodes.append(node_id)
            self.available_nodes.sort()
        mode = self.complete_modes.get(operation_id, "success")
        if mode == "node-loss":
            self.complete_modes[operation_id] = "success"
            return self._save(
                replace(record, state=OperationState.WAITING_FOR_NODE, node_id=None)
            )
        if mode == "credential":
            self.complete_modes[operation_id] = "success"
            return self._save(
                replace(record, state=OperationState.BLOCKED_CREDENTIAL, node_id=None)
            )
        if mode == "uncertain":
            return self._save(replace(record, state=OperationState.RECONCILE_REQUIRED))
        if mode == "rejected":
            return self._save(replace(record, state=OperationState.REJECTED))
        return self._save(
            replace(
                record,
                state=OperationState.SUCCEEDED,
                evidence_refs=record.evidence_refs + ("provider:healthy",),
            )
        )

    def retry(self, operation_id: str) -> DeploymentExecutionRecord:
        self.calls.append(("retry", operation_id))
        return self.prepare(operation_id)

    def reconcile(self, operation_id: str) -> DeploymentExecutionRecord:
        self.calls.append(("reconcile", operation_id))
        record = self.records[operation_id]
        self.complete_modes[operation_id] = "success"
        return self._save(
            replace(
                record,
                state=OperationState.SUCCEEDED,
                evidence_refs=record.evidence_refs + ("provider:reconciled",),
            )
        )

    def get(self, operation_id: str) -> DeploymentExecutionRecord:
        return self.records[operation_id]

    def _save(self, record: DeploymentExecutionRecord) -> DeploymentExecutionRecord:
        self.records[record.spec.operation_id] = record
        return record


def _sha(seed: int) -> str:
    return f"{seed:064x}"[-64:]


def _digest(seed: int) -> str:
    return f"{seed + 10_000:064x}"[-64:]


def _replica(
    service_id: str,
    replica: int,
    *,
    project_id: str = "project-social",
    environment_id: str = "prod-eu",
    release_seed: int = 1,
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
        operation_id=f"op-{suffix}",
        request=ExecutionRequest(
            project_id,
            f"work-{suffix}",
            Platform.LINUX,
            frozenset({"deploy"}),
            frozenset({"ansible"}),
            ResourceEnvelope(1, 256, 512),
        ),
        intent=DeploymentIntent(
            f"intent-{suffix}",
            project_id,
            environment,
            release,
        ),
        credential_ref="cred:deploy",
        credential_audience="provider",
        credential_scope="deploy",
    )


def _service(
    service_id: str,
    *,
    wave: int = 0,
    replicas: int = 3,
    minimum: int | None = None,
    depends_on: tuple[str, ...] = (),
    release_seed: int = 1,
) -> ServiceFleetSpec:
    return ServiceFleetSpec(
        service_id,
        wave,
        tuple(
            _replica(service_id, index, release_seed=release_seed)
            for index in range(replicas)
        ),
        minimum if minimum is not None else replicas,
        depends_on,
    )


def test_service_rejects_release_drift_across_replicas() -> None:
    replicas = list(_service("api").replicas)
    drift = replicas[1]
    replicas[1] = replace(
        drift,
        intent=replace(
            drift.intent,
            release=ReleaseRef(
                drift.intent.project_id,
                "1.0.1",
                _sha(99),
                _digest(99),
            ),
        ),
    )
    with pytest.raises(DeploymentFleetError, match="exact project, environment and release"):
        ServiceFleetSpec("api", 0, tuple(replicas), 2)


def test_plan_rejects_duplicate_work_identity_and_same_wave_dependency() -> None:
    parent = _service("parent", wave=0)
    child = _service("child", wave=0, depends_on=("parent",))
    with pytest.raises(DeploymentFleetError, match="earlier wave"):
        DeploymentFleetPlan("fleet", "project-social", (parent, child))

    first, second = _service("dup", replicas=2).replicas
    duplicate = replace(second, request=replace(second.request, work_id=first.request.work_id))
    with pytest.raises(DeploymentFleetError, match="unique work identities"):
        ServiceFleetSpec("dup", 0, (first, duplicate), 1)


def test_capacity_reservation_spreads_replicas_and_recovers_shortage() -> None:
    execution = FakeExecution(node_count=3)
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(
        DeploymentFleetPlan(
            "fleet-capacity",
            "project-social",
            (_service("messages", replicas=4, minimum=3),),
        )
    )

    first = coordinator.advance("fleet-capacity")
    service = first.services[0]
    assert service.state is FleetState.DEGRADED
    assert service.healthy_replicas == 3
    assert len({replica.node_id for replica in service.replicas if replica.node_id}) == 3
    waiting = [
        replica
        for replica in service.replicas
        if replica.state is OperationState.WAITING_FOR_NODE
    ]
    assert len(waiting) == 1

    second = coordinator.advance("fleet-capacity")
    assert second.state is FleetState.HEALTHY
    assert second.services[0].healthy_replicas == 4


def test_partial_node_loss_and_credential_block_do_not_corrupt_parallel_service() -> None:
    execution = FakeExecution(node_count=8)
    messages = _service("messages", replicas=3, minimum=2)
    profiles = _service("profiles", replicas=3, minimum=2)
    execution.complete_modes[messages.replicas[0].operation_id] = "node-loss"
    execution.complete_modes[messages.replicas[1].operation_id] = "credential"
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(
        DeploymentFleetPlan("fleet-isolation", "project-social", (messages, profiles))
    )

    first = coordinator.advance("fleet-isolation")
    by_id = {service.service_id: service for service in first.services}
    assert by_id["messages"].state is FleetState.BLOCKED
    assert by_id["messages"].healthy_replicas == 1
    assert by_id["profiles"].state is FleetState.HEALTHY
    assert by_id["profiles"].healthy_replicas == 3

    second = coordinator.advance("fleet-isolation")
    by_id = {service.service_id: service for service in second.services}
    assert by_id["messages"].state is FleetState.HEALTHY
    assert by_id["profiles"].state is FleetState.HEALTHY


def test_uncertain_replica_reconciles_without_second_completion() -> None:
    execution = FakeExecution(node_count=4)
    service = _service("search", replicas=2, minimum=2)
    uncertain_id = service.replicas[0].operation_id
    execution.complete_modes[uncertain_id] = "uncertain"
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(DeploymentFleetPlan("fleet-reconcile", "project-social", (service,)))

    first = coordinator.advance("fleet-reconcile")
    assert first.state is FleetState.RECONCILE_REQUIRED
    assert execution.calls.count(("complete", uncertain_id)) == 1

    second = coordinator.advance("fleet-reconcile")
    assert second.state is FleetState.HEALTHY
    assert execution.calls.count(("complete", uncertain_id)) == 1
    assert execution.calls.count(("reconcile", uncertain_id)) == 1


def test_dependency_waits_for_required_parent_health() -> None:
    execution = FakeExecution(node_count=8)
    parent = _service("db", wave=0, replicas=2, minimum=2)
    child = _service("api", wave=1, replicas=2, minimum=2, depends_on=("db",))
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(
        DeploymentFleetPlan("fleet-deps", "project-social", (parent, child))
    )

    first = coordinator.advance("fleet-deps")
    by_id = {service.service_id: service for service in first.services}
    assert by_id["db"].state is FleetState.HEALTHY
    assert by_id["api"].state is FleetState.PENDING

    second = coordinator.advance("fleet-deps")
    assert second.state is FleetState.HEALTHY


def test_failed_parent_blocks_only_dependents() -> None:
    execution = FakeExecution(node_count=12)
    parent = _service("db", wave=0, replicas=2, minimum=2)
    child = _service("api", wave=1, replicas=2, minimum=2, depends_on=("db",))
    unrelated = _service("media", wave=1, replicas=2, minimum=2)
    execution.complete_modes[parent.replicas[0].operation_id] = "rejected"
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(
        DeploymentFleetPlan("fleet-failure", "project-social", (parent, child, unrelated))
    )

    coordinator.advance("fleet-failure")
    second = coordinator.advance("fleet-failure")
    by_id = {service.service_id: service for service in second.services}
    assert by_id["db"].state is FleetState.FAILED
    assert by_id["api"].state is FleetState.PENDING
    assert by_id["media"].state is FleetState.HEALTHY


def test_snapshot_restore_requires_exact_underlying_execution_specs() -> None:
    execution = FakeExecution()
    plan = DeploymentFleetPlan(
        "fleet-snapshot",
        "project-social",
        (_service("messages", replicas=2),),
    )
    original = DeploymentFleetCoordinator(execution)
    original.submit(plan)
    snapshot = original.snapshot()

    restored = DeploymentFleetCoordinator(execution)
    restored.restore(snapshot)
    assert restored.get("fleet-snapshot").plan == plan

    operation_id = plan.services[0].replicas[0].operation_id
    execution.records[operation_id] = replace(
        execution.records[operation_id],
        spec=replace(execution.records[operation_id].spec, credential_scope="other"),
    )
    with pytest.raises(DeploymentFleetError, match="disagrees with execution state"):
        DeploymentFleetCoordinator(execution).restore(snapshot)


def test_snapshot_rejects_duplicate_plan_identity() -> None:
    execution = FakeExecution()
    plan = DeploymentFleetPlan("fleet-snapshot", "project-social", (_service("api"),))
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(plan)
    snapshot = DeploymentFleetSnapshot((plan, plan))
    with pytest.raises(DeploymentFleetError, match="duplicate plans"):
        coordinator.restore(snapshot)


def test_scale_60_services_180_replicas_three_waves_restart_safe() -> None:
    execution = FakeExecution(node_count=256)
    services: list[ServiceFleetSpec] = []
    for index in range(60):
        wave = index // 20
        depends = () if wave == 0 else (f"service-{index - 20:02d}",)
        services.append(
            _service(
                f"service-{index:02d}",
                wave=wave,
                replicas=3,
                minimum=2,
                depends_on=depends,
                release_seed=index + 1,
            )
        )
    plan = DeploymentFleetPlan("fleet-scale", "project-social", tuple(services))
    coordinator = DeploymentFleetCoordinator(execution)
    coordinator.submit(plan)

    after_wave_0 = coordinator.advance("fleet-scale")
    assert sum(service.state is FleetState.HEALTHY for service in after_wave_0.services) == 20

    snapshot = coordinator.snapshot()
    restarted = DeploymentFleetCoordinator(execution)
    restarted.restore(snapshot)
    after_wave_1 = restarted.advance("fleet-scale")
    assert sum(service.state is FleetState.HEALTHY for service in after_wave_1.services) == 40

    final = restarted.advance("fleet-scale")
    assert final.state is FleetState.HEALTHY
    assert len(final.services) == 60
    assert sum(len(service.replicas) for service in final.services) == 180
    assert all(
        service.release_sha == _sha(index + 1)
        for index, service in enumerate(final.services)
    )
