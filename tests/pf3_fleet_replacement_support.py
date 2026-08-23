from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
)
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
    DurableReplacementDispatch,
    EnvironmentReplacementBudget,
    FleetReplacementCoordinator,
    FleetReplacementError,
    FleetReplacementPlan,
    ReplicaReplacementResult,
    ReplicaReplacementSpec,
    fleet_replacement_plan_fingerprint,
    fleet_replacement_request_fingerprint,
    fleet_replacement_result_fingerprint,
)
from nika_core.product_factory_incidents import TrustedReviewAuthority
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

REVIEW_REF = "review:fleet-replacement:accepted"
ACCEPTANCE_COMMAND = (
    "python",
    "-m",
    "pytest",
    "tests/test_product_factory_fleet_replacement.py",
)


class FakeFleet:
    def __init__(self, record: DeploymentFleetRecord) -> None:
        self.record = record

    def get(self, plan_id: str) -> DeploymentFleetRecord:
        if plan_id != self.record.plan.plan_id:
            raise AssertionError("unknown fake fleet plan")
        return self.record


class FakeDispatchJournal:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], DurableReplacementDispatch] = {}

    def prepare(self, request, *, attempt: int, source_was_enabled: bool):
        key = (request.plan_id, request.service_id, request.replica_id)
        candidate = DurableReplacementDispatch(
            request=request,
            attempt=attempt,
            source_was_enabled=source_was_enabled,
            request_checksum_sha256=fleet_replacement_request_fingerprint(request),
        )
        existing = self.records.get(key)
        if existing is not None:
            if (
                existing.request != candidate.request
                or existing.attempt != candidate.attempt
                or existing.source_was_enabled is not candidate.source_was_enabled
            ):
                raise FleetReplacementError("fake durable dispatch conflicts with prior effect")
            return existing
        self.records[key] = candidate
        return candidate

    def record_terminal(self, request, result):
        key = (request.plan_id, request.service_id, request.replica_id)
        existing = self.records.get(key)
        if existing is None or existing.request != request:
            raise FleetReplacementError("fake terminal result has no durable dispatch")
        candidate = DurableReplacementDispatch(
            request=request,
            attempt=existing.attempt,
            source_was_enabled=existing.source_was_enabled,
            request_checksum_sha256=existing.request_checksum_sha256,
            terminal_result=result,
            result_checksum_sha256=fleet_replacement_result_fingerprint(result),
        )
        if existing.terminal_result is not None and existing != candidate:
            raise FleetReplacementError("fake terminal result conflicts with prior evidence")
        self.records[key] = candidate
        return candidate

    def list_plan(self, plan_id: str):
        return tuple(
            self.records[key]
            for key in sorted(self.records)
            if key[0] == plan_id
        )


class FakeReplacementPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.requests = []
        self.modes: dict[str, str] = {}

    def apply(self, request):
        self.calls.append(("apply", request.request_id))
        self.requests.append(request)
        return self._result(request, self.modes.get(request.request_id, "success"))

    def inspect(self, request):
        self.calls.append(("inspect", request.request_id))
        self.requests.append(request)
        mode = self.modes.get(request.request_id, "success")
        if mode == "uncertain":
            self.modes[request.request_id] = "success"
            mode = "success"
        return self._result(request, mode)

    @staticmethod
    def _result(request, mode: str):
        if mode == "raise":
            raise RuntimeError("provider transport detail must not become durable evidence")
        if mode == "hard-crash":
            raise SystemExit("simulated process death after provider effect")
        if mode == "uncertain":
            return ReplicaReplacementResult(False, True, ("provider:uncertain",))
        if mode == "reject":
            return ReplicaReplacementResult(False, False, ("provider:rejected",))
        if mode == "wrong-release":
            return ReplicaReplacementResult(
                applied=True,
                uncertain=False,
                evidence_refs=("provider:wrong-release",),
                observed_node_id=request.target_node_id,
                release_version=request.release_version,
                release_sha=_sha(999_999),
                artifact_digest=request.artifact_digest,
                healthy=True,
            )
        if mode == "wrong-version":
            return ReplicaReplacementResult(
                applied=True,
                uncertain=False,
                evidence_refs=("provider:wrong-version",),
                observed_node_id=request.target_node_id,
                release_version="9.9.9",
                release_sha=request.release_sha,
                artifact_digest=request.artifact_digest,
                healthy=True,
            )
        return ReplicaReplacementResult(
            applied=True,
            uncertain=False,
            evidence_refs=("provider:replacement:healthy",),
            observed_node_id=request.target_node_id,
            release_version=request.release_version,
            release_sha=request.release_sha,
            artifact_digest=request.artifact_digest,
            healthy=True,
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
        "provider:authorized-adapter",
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
    dispatch_journal=None,
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
                datetime(2026, 8, 23, 10, service_index % 60, tzinfo=UTC),
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
    journal = FakeDispatchJournal() if dispatch_journal is None else dispatch_journal
    coordinator = FleetReplacementCoordinator(fleet, operations, nodes, port, journal)
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


def _authorization_coordinator() -> tuple[ProductFactoryCoordinator, str]:
    graph = ProductRepositoryGraph(
        "project-social",
        (RepositoryRef("repo-ops", "github", "example/fleet-ops", "main"),),
        (
            ProductComponent(
                "fleet-ops",
                "repo-ops",
                ("src/fleet_ops",),
                test_commands=(ACCEPTANCE_COMMAND,),
            ),
        ),
    )
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-ops": _sha(700)},
        goals={"fleet-ops": "Review and authorize exact fleet replacement plan"},
        permission_ceiling=frozenset({"read_source", "run_tests"}),
    )
    request = coordinator.ready_requests()[0]
    return coordinator, request.work_id


def _authorized_plan(
    placements: dict[tuple[str, str], tuple[str, str]],
    keys: tuple[tuple[str, str], ...],
    *,
    max_unavailable: int = 4,
    max_concurrent: int = 2,
    max_replicas_per_node: int = 100,
    retain_cordoned_nodes: tuple[str, ...] = (),
):
    review_coordinator, authorization_work_id = _authorization_coordinator()
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
    plan = FleetReplacementPlan(
        "replacement-001",
        "project-social",
        "fleet-production",
        replacements,
        budgets,
        max_replicas_per_node,
        authorization_work_id,
        "approval:audit-metadata-only",
        "capacity rebalance and rolling replacement",
        ("change:replacement-001",),
        retain_cordoned_nodes,
    )
    request = review_coordinator.start("fleet-ops")
    coding_result = CodingResult(
        request.work_id,
        test_evidence=(TestEvidence(ACCEPTANCE_COMMAND, 0, _digest(900)),),
    )
    review_coordinator.record_result(
        WorkerResultEnvelope(
            request.work_id,
            request.component_id,
            request.repository_id,
            request.base_sha,
            _sha(701),
            _digest(901),
            coding_result,
        )
    )
    review_coordinator.review(
        "fleet-ops",
        ReviewDecision(
            "reviewer:independent-qa",
            True,
            "exact replacement plan accepted",
            (
                REVIEW_REF,
                f"fleet-replacement-plan:{fleet_replacement_plan_fingerprint(plan)}",
            ),
        ),
    )
    authority = TrustedReviewAuthority(
        review_coordinator.snapshot(),
        review_coordinator.trusted_plan_fingerprint,
    )
    return plan, authority, REVIEW_REF


def _submit(coordinator, plan, authority, review_ref=REVIEW_REF):
    return coordinator.submit(
        plan,
        review_authority=authority,
        review_ref=review_ref,
    )
