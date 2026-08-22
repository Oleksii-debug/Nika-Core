from __future__ import annotations

from datetime import UTC, datetime

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
from nika_core.product_factory_fleet_replacement import (
    EnvironmentReplacementBudget,
    FleetReplacementCoordinator,
    FleetReplacementPlan,
    ReplicaReplacementResult,
    ReplicaReplacementSpec,
)
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceObservation,
    ServiceReplica,
)


class FakeFleet:
    def __init__(self, record: DeploymentFleetRecord) -> None:
        self.record = record

    def get(self, plan_id: str) -> DeploymentFleetRecord:
        if plan_id != self.record.plan.plan_id:
            raise AssertionError("unknown fake fleet plan")
        return self.record


class FakeReplacementPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.modes: dict[str, str] = {}

    def apply(self, request):
        self.calls.append(("apply", request.request_id))
        return self._result(request, self.modes.get(request.request_id, "success"))

    def inspect(self, request):
        self.calls.append(("inspect", request.request_id))
        mode = self.modes.get(request.request_id, "success")
        if mode == "uncertain":
            self.modes[request.request_id] = "success"
            mode = "success"
        return self._result(request, mode)

    @staticmethod
    def _result(request, mode: str):
        if mode == "raise":
            raise RuntimeError("provider transport detail must not become durable evidence")
        if mode == "uncertain":
            return ReplicaReplacementResult(False, True, ("provider:uncertain",))
        if mode == "reject":
            return ReplicaReplacementResult(False, False, ("provider:rejected",))
        if mode == "wrong-release":
            return ReplicaReplacementResult(
                True,
                False,
                ("provider:wrong-release",),
                request.target_node_id,
                _sha(999_999),
                request.artifact_digest,
                True,
            )
        if mode == "wrong-target":
            return ReplicaReplacementResult(
                True,
                False,
                ("provider:wrong-target",),
                request.source_node_id,
                request.release_sha,
                request.artifact_digest,
                True,
            )
        return ReplicaReplacementResult(
            True,
            False,
            ("provider:replacement:healthy",),
            request.target_node_id,
            request.release_sha,
            request.artifact_digest,
            True,
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
        f"deploy-{suffix}",
        ExecutionRequest(
            project_id,
            f"deploy-work-{suffix}",
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


def _node(index: int, *, memory_mb: int = 16_384) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(
            f"node-{index}",
            Platform.LINUX,
            "x86_64",
            f"instance-{index}",
        ),
        NodeCapabilities(
            frozenset({"deploy", "replacement"}),
            frozenset({"ansible"}),
        ),
        ResourceEnvelope(8, memory_mb, 100_000),
    )


def _fixture(
    *,
    service_count: int = 4,
    replica_count: int = 3,
    node_count: int = 6,
    environments: tuple[str, ...] = ("prod-eu", "prod-us"),
):
    project_id = "project-social"
    nodes = ExecutionNodeRegistry()
    for index in range(node_count):
        nodes.register(_node(index))

    operations = ProductOperationsCoordinator(project_id)
    service_specs: list[ServiceFleetSpec] = []
    service_records: list[ServiceFleetRecord] = []
    placements: dict[tuple[str, str], tuple[str, str]] = {}
    for service_index in range(service_count):
        service_id = f"service-{service_index:03d}"
        environment_id = environments[service_index % len(environments)]
        release_seed = service_index + 1
        execution_specs = tuple(
            _execution_spec(
                service_id,
                replica,
                project_id=project_id,
                environment_id=environment_id,
                release_seed=release_seed,
            )
            for replica in range(replica_count)
        )
        service_spec = ServiceFleetSpec(
            service_id,
            service_index // 20,
            execution_specs,
            max(1, replica_count - 1),
        )
        service_specs.append(service_spec)

        fleet_replicas: list[ReplicaFleetRecord] = []
        ops_replicas: list[ServiceReplica] = []
        for replica_index, execution_spec in enumerate(execution_specs):
            node_id = f"node-{(service_index + replica_index) % node_count}"
            replica_id = f"{service_id}-replica-{replica_index}"
            fleet_replicas.append(
                ReplicaFleetRecord(
                    execution_spec.operation_id,
                    execution_spec.request.work_id,
                    OperationState.SUCCEEDED,
                    node_id,
                    1,
                    ("provider:healthy",),
                )
            )
            ops_replicas.append(ServiceReplica(replica_id, node_id))
            placements[(service_id, replica_id)] = (execution_spec.operation_id, node_id)

        service_records.append(
            ServiceFleetRecord(
                service_id,
                service_spec.release.source_sha,
                service_spec.release.artifact_digest,
                environment_id,
                FleetState.HEALTHY,
                replica_count,
                service_spec.min_healthy_replicas,
                tuple(fleet_replicas),
            )
        )
        operations.register(
            DeployableService(
                service_id,
                project_id,
                environment_id,
                service_spec.release.source_sha,
                service_spec.wave,
                tuple(ops_replicas),
                service_spec.min_healthy_replicas,
                credential_refs=("cred:deploy",),
            )
        )
        operations.record_observation(
            ServiceObservation(
                service_id,
                service_spec.release.source_sha,
                tuple(replica.replica_id for replica in ops_replicas),
                (),
                (f"health:{service_id}",),
                datetime(2026, 8, 21, 11, service_index % 60, tzinfo=UTC),
            )
        )

    fleet_plan = DeploymentFleetPlan("fleet-production", project_id, tuple(service_specs))
    fleet = FakeFleet(
        DeploymentFleetRecord(
            fleet_plan,
            FleetState.HEALTHY,
            tuple(service_records),
        )
    )
    port = FakeReplacementPort()
    coordinator = FleetReplacementCoordinator(fleet, operations, nodes, port)
    return coordinator, fleet, operations, nodes, port, placements


def _replacement_spec(
    service_id: str,
    replica_id: str,
    operation_id: str,
    source_node_id: str,
    *,
    memory_mb: int = 512,
) -> ReplicaReplacementSpec:
    return ReplicaReplacementSpec(
        service_id,
        replica_id,
        operation_id,
        source_node_id,
        ExecutionRequest(
            "project-social",
            f"replacement-work:{service_id}:{replica_id}",
            Platform.LINUX,
            frozenset({"replacement"}),
            frozenset({"ansible"}),
            ResourceEnvelope(1, memory_mb, 512),
        ),
    )


def _plan(
    placements: dict[tuple[str, str], tuple[str, str]],
    keys: tuple[tuple[str, str], ...],
    *,
    max_unavailable: int = 4,
    max_concurrent: int = 2,
    max_replicas_per_node: int = 100,
    retain_cordoned_nodes: tuple[str, ...] = (),
) -> FleetReplacementPlan:
    replacements = tuple(
        _replacement_spec(
            service_id,
            replica_id,
            placements[(service_id, replica_id)][0],
            placements[(service_id, replica_id)][1],
        )
        for service_id, replica_id in keys
    )
    environments = {
        "prod-eu" if int(service_id.rsplit("-", 1)[1]) % 2 == 0 else "prod-us"
        for service_id, _ in keys
    }
    budgets = tuple(
        EnvironmentReplacementBudget(environment_id, max_unavailable, max_concurrent)
        for environment_id in sorted(environments)
    )
    return FleetReplacementPlan(
        "replacement-001",
        "project-social",
        "fleet-production",
        replacements,
        budgets,
        max_replicas_per_node,
        "approval:maintenance-window-77",
        "capacity rebalance and rolling replacement",
        ("change:replacement-001",),
        retain_cordoned_nodes,
    )
