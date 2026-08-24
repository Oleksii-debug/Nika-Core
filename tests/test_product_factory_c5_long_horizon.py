from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryRecoveryDisposition,
)
from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    SecretRef,
)
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    HealthEvidence,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    ResourceEnvelope,
    RollbackEvidence,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionCoordinator,
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_factory_incident_contracts import (
    IncidentKind,
    IncidentSeverity,
    IncidentState,
    IncidentTrigger,
)
from nika_core.product_factory_incidents import IncidentRepairReleaseCoordinator
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    RollbackObservation,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductArchitectureDecision,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ProductRequirementKind,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

PROJECT_ID = "c5-long-horizon"
REPOSITORY_LOCATOR = "org/c5-product"
SECRET_REF = "secret:c5:deployment"
NOW = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)

SHA_BASE = "1" * 40
SHA_REPAIR = "2" * 40
SHA_DOCS = "3" * 40
SHA_UI = "4" * 40
SHA_V2_BASE = "5" * 40
RELEASE_V1 = "6" * 40
RELEASE_V2 = "7" * 40
DIFF_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64
ARTIFACT_V1 = "a" * 64
ARTIFACT_V2 = "b" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


@dataclass
class FakeProtectedStore:
    generations: set[tuple[str, int]] = field(default_factory=set)
    revoked: list[tuple[str, int]] = field(default_factory=list)

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self.generations

    def issue_handle(self, **kwargs: object) -> str:
        return f"opaque:{kwargs['secret_ref']}:{kwargs['generation']}"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        self.revoked.append((secret_ref, generation))


@dataclass
class FakeNodeHealth:
    unavailable: set[str] = field(default_factory=set)

    def is_available(self, node_id: str) -> bool:
        return node_id not in self.unavailable


@dataclass
class FakeDeploymentProvider:
    unhealthy_intents: set[str] = field(default_factory=set)
    deploy_calls: list[str] = field(default_factory=list)
    rollback_calls: list[str] = field(default_factory=list)

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls.append(intent.intent_id)
        return ProviderDeploymentResult(
            applied=True,
            uncertain=False,
            evidence_refs=(f"provider:deploy:{intent.intent_id}",),
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.intent_id not in self.unhealthy_intents,
            (f"provider:health:{intent.intent_id}",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        self.rollback_calls.append(intent.intent_id)
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            (f"provider:rollback:{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            (f"provider:inspect:{intent.intent_id}",),
        )


@dataclass
class FakeOperationsPort:
    apply_calls: list[str] = field(default_factory=list)

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.apply_calls.append(request.request_id)
        return MaintenanceResult(True, False, (f"maintenance:{request.request_id}",))

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        return MaintenanceResult(True, False, (f"maintenance:inspect:{request.request_id}",))


def _project_spec(goal_suffix: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=f"Build durable mixed-platform product {goal_suffix}",
        desired_outcome="One ProductProject survives implementation, release and operations",
        requirements=(
            ProductRequirement(
                "req-runtime",
                "Support Linux and Windows execution targets",
                ("Both platform execution paths pass deterministic acceptance",),
                kind=ProductRequirementKind.PLATFORM,
            ),
            ProductRequirement(
                "req-security",
                "Independent security review is mandatory",
                ("Rejected candidates cannot become accepted work",),
                kind=ProductRequirementKind.SECURITY,
            ),
        ),
        architecture_decision_refs=("adr:c5:runtime",),
        repository_refs=(REPOSITORY_LOCATOR,),
        team_refs=("team:implementation", "team:security-review"),
        credential_refs=(SECRET_REF,),
        architecture_decisions=(
            ProductArchitectureDecision(
                "adr-c5-runtime",
                "Reuse Product Factory production contracts",
                "C5 composes existing PF0/PF2/PF3/PF8 authorities without a second FSM",
            ),
        ),
    )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef("repo-product", "github", REPOSITORY_LOCATOR, "main"),
        ),
        components=(
            ProductComponent(
                "core",
                "repo-product",
                ("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                "docs",
                "repo-product",
                ("docs/product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
            ProductComponent(
                "ui",
                "repo-product",
                ("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _success(request, result_sha: str) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        request.work_id,
        request.component_id,
        request.repository_id,
        request.base_sha,
        result_sha,
        DIFF_DIGEST,
        CodingResult(
            job_id=request.work_id,
            test_evidence=(TestEvidence(request.acceptance_commands[0], 0, TEST_DIGEST),),
        ),
    )


def _accept_component(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
    result_sha: str,
    reviewer: str,
) -> None:
    request = coordinator.start(component_id)
    coordinator.record_result(_success(request, result_sha))
    record = coordinator.review(
        component_id,
        ReviewDecision(reviewer, True, "accepted by independent review", (f"review:{reviewer}",)),
    )
    assert record.state is WorkState.ACCEPTED


def _node(
    node_id: str,
    platform: Platform,
    instance_id: str,
    *,
    enabled: bool = True,
) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(node_id, platform, "x86_64", instance_id),
        NodeCapabilities(frozenset({"deploy"}), frozenset({"python"})),
        ResourceEnvelope(2, 2048, 4096),
        enabled,
    )


def _execution_spec(
    *,
    operation_id: str,
    platform: Platform,
    environment_id: str,
    tier: EnvironmentTier,
    release_sha: str,
    artifact_digest: str,
    audience: str,
    scope: str,
) -> DeploymentExecutionSpec:
    intent = DeploymentIntent(
        intent_id=f"intent:{operation_id}",
        project_id=PROJECT_ID,
        environment=EnvironmentIdentity(
            environment_id,
            PROJECT_ID,
            tier,
            f"provider:{tier.value}",
        ),
        release=ReleaseRef(
            PROJECT_ID,
            f"version-{release_sha[0]}",
            release_sha,
            artifact_digest,
        ),
    )
    return DeploymentExecutionSpec(
        operation_id,
        ExecutionRequest(
            PROJECT_ID,
            f"work:{operation_id}",
            platform,
            frozenset({"deploy"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 256, 256),
        ),
        intent,
        SECRET_REF,
        audience,
        scope,
    )


def test_c5_product_project_survives_long_horizon_mixed_platform_lifecycle(tmp_path) -> None:
    db = tmp_path / "nika-c5.db"
    store = SQLiteStore(db)
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id=PROJECT_ID,
        name="C5 Product",
        spec=_project_spec("v1"),
        idempotency_key="c5:create",
    )
    graph = _graph()
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="workspace:c5",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    coordinator = binding.plan(
        base_shas={"repo-product": SHA_BASE},
        component_goals={
            "core": "implement core",
            "docs": "document product",
            "ui": "implement accessible UI",
        },
        permission_ceiling=PERMISSIONS,
    )
    checkpoint_host = ProductFactoryCheckpointHost(store)
    checkpoint_host.save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    first_request = coordinator.start("core")
    first_result = _success(first_request, SHA_REPAIR)
    coordinator.record_result(first_result)
    rejected = coordinator.review(
        "core",
        ReviewDecision(
            "security-reviewer",
            False,
            "unsafe capability expansion",
            ("security:review:rejected",),
        ),
    )
    assert rejected.state is WorkState.REPAIR_REQUIRED
    checkpoint_host.save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    restarted_store = SQLiteStore(db)
    restarted_store.initialize()
    restarted_projects = ProductProjectRepository(restarted_store)
    restarted_project = restarted_projects.get(PROJECT_ID)
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, graph)
    restarted_host = ProductFactoryCheckpointHost(restarted_store)
    coordinator = restarted_host.restore_latest(
        host_task_id=task.task_id,
        binding=restarted_binding,
    )
    assert coordinator.snapshot().project_id == PROJECT_ID

    repair = coordinator.prepare_repair(
        "core",
        base_sha=SHA_BASE,
        reason="remove rejected capability expansion",
    )
    assert repair.attempt == 2
    restarted_host.save(
        host_task_id=task.task_id,
        checkpoint=restarted_binding.checkpoint(coordinator),
    )
    coordinator.start("core")
    with pytest.raises(CoordinatorError, match="worker result"):
        coordinator.record_result(first_result)
    coordinator.record_result(_success(repair, SHA_REPAIR))
    coordinator.review(
        "core",
        ReviewDecision(
            "security-reviewer",
            True,
            "repair preserves permission ceiling",
            ("security:review:accepted",),
        ),
    )
    restarted_host.save(
        host_task_id=task.task_id,
        checkpoint=restarted_binding.checkpoint(coordinator),
    )

    _accept_component(coordinator, "docs", SHA_DOCS, "docs-reviewer")
    restarted_host.save(
        host_task_id=task.task_id,
        checkpoint=restarted_binding.checkpoint(coordinator),
    )
    _accept_component(coordinator, "ui", SHA_UI, "ui-security-reviewer")
    restarted_host.save(
        host_task_id=task.task_id,
        checkpoint=restarted_binding.checkpoint(coordinator),
    )
    assert all(record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records)

    current = restarted_projects.get(PROJECT_ID)
    updated = restarted_projects.update_spec(
        PROJECT_ID,
        _project_spec("v2 with offline requirement"),
        expected_row_version=current.row_version,
        change_reason="add offline durability requirement",
    )
    updated_binding = ProductProjectCoordinatorBinding(updated, graph)
    stale = restarted_host.inspect_latest(
        host_task_id=task.task_id,
        binding=updated_binding,
    )
    assert stale.disposition is ProductFactoryRecoveryDisposition.STALE_PROJECT

    v2_task = TaskQueue(restarted_store).create(
        workspace_id="workspace:c5",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    v2_coordinator = updated_binding.plan(
        base_shas={"repo-product": SHA_V2_BASE},
        component_goals={
            "core": "adapt core to v2",
            "docs": "document v2",
            "ui": "adapt UI to v2",
        },
        permission_ceiling=PERMISSIONS,
    )
    restarted_host.save(
        host_task_id=v2_task.task_id,
        checkpoint=updated_binding.checkpoint(v2_coordinator),
    )

    secret_store = FakeProtectedStore({(SECRET_REF, 1)})
    credentials = CredentialBroker(secret_store)
    credentials.register_secret(
        SecretRef(
            SECRET_REF,
            PROJECT_ID,
            "fixture-provider",
            "deployment",
            frozenset({"deploy:staging", "deploy:production"}),
            frozenset({"provider-staging", "provider-production"}),
        ),
        now=NOW,
    )
    nodes = ExecutionNodeRegistry()
    nodes.register(_node("linux-a", Platform.LINUX, "linux-a-1"))
    nodes.register(_node("linux-b", Platform.LINUX, "linux-b-1", enabled=False))
    nodes.register(_node("windows-a", Platform.WINDOWS, "windows-a-1"))
    node_health = FakeNodeHealth()
    provider = FakeDeploymentProvider()
    fabric = DeploymentFabric(provider)
    execution = DeploymentExecutionCoordinator(nodes, credentials, fabric, node_health)

    linux_stage = _execution_spec(
        operation_id="stage-linux-v1",
        platform=Platform.LINUX,
        environment_id="staging-linux",
        tier=EnvironmentTier.STAGING,
        release_sha=RELEASE_V1,
        artifact_digest=ARTIFACT_V1,
        audience="provider-staging",
        scope="deploy:staging",
    )
    execution.submit(linux_stage, now=NOW)
    prepared_linux = execution.prepare(linux_stage.operation_id, now=NOW)
    assert prepared_linux.node_id == "linux-a"
    node_health.unavailable.add("linux-a")
    waiting = execution.complete(linux_stage.operation_id, now=NOW)
    assert waiting.state is OperationState.WAITING_FOR_NODE
    assert provider.deploy_calls == []
    nodes.register(_node("linux-a", Platform.LINUX, "linux-a-1", enabled=False))
    nodes.register(_node("linux-b", Platform.LINUX, "linux-b-1"))
    retried_linux = execution.retry(linux_stage.operation_id, now=NOW)
    assert retried_linux.node_id == "linux-b"
    assert execution.complete(linux_stage.operation_id, now=NOW).state is OperationState.SUCCEEDED

    windows_stage = _execution_spec(
        operation_id="stage-windows-v1",
        platform=Platform.WINDOWS,
        environment_id="staging-windows",
        tier=EnvironmentTier.STAGING,
        release_sha=RELEASE_V1,
        artifact_digest=ARTIFACT_V1,
        audience="provider-staging",
        scope="deploy:staging",
    )
    execution.submit(windows_stage, now=NOW)
    assert execution.prepare(windows_stage.operation_id, now=NOW).node_id == "windows-a"
    stale_lease = credentials.issue_lease(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        audience="provider-staging",
        scopes=frozenset({"deploy:staging"}),
        now=NOW,
    )
    credentials.revoke(project_id=PROJECT_ID, secret_ref=SECRET_REF, now=NOW)
    assert execution.complete(windows_stage.operation_id, now=NOW).state is OperationState.BLOCKED_CREDENTIAL
    secret_store.generations.add((SECRET_REF, 2))
    restored_secret = credentials.rotate(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        now=NOW + timedelta(seconds=1),
    )
    assert restored_secret.generation == 2
    with pytest.raises(CredentialBrokerError, match="unknown or invalidated"):
        credentials.authorize_use(
            lease_id=stale_lease.lease_id,
            project_id=PROJECT_ID,
            scope="deploy:staging",
            now=NOW + timedelta(seconds=1),
        )
    assert execution.retry(windows_stage.operation_id, now=NOW + timedelta(seconds=1)).node_id == "windows-a"
    assert (
        execution.complete(windows_stage.operation_id, now=NOW + timedelta(seconds=1)).state
        is OperationState.SUCCEEDED
    )

    node_snapshot = nodes.snapshot()
    credential_snapshot = credentials.snapshot()
    fabric_snapshot = fabric.snapshot()
    execution_snapshot = execution.snapshot()

    nodes = ExecutionNodeRegistry()
    nodes.restore(node_snapshot)
    credentials = CredentialBroker(secret_store)
    credentials.restore(credential_snapshot)
    fabric = DeploymentFabric(provider)
    fabric.restore(fabric_snapshot)
    execution = DeploymentExecutionCoordinator(nodes, credentials, fabric, node_health)
    execution.restore(execution_snapshot)

    production_v1 = _execution_spec(
        operation_id="production-v1",
        platform=Platform.LINUX,
        environment_id="production",
        tier=EnvironmentTier.PRODUCTION,
        release_sha=RELEASE_V1,
        artifact_digest=ARTIFACT_V1,
        audience="provider-production",
        scope="deploy:production",
    )
    execution.submit(production_v1, now=NOW + timedelta(minutes=1))
    execution.prepare(production_v1.operation_id, now=NOW + timedelta(minutes=1))
    released = execution.complete(production_v1.operation_id, now=NOW + timedelta(minutes=1))
    assert released.state is OperationState.SUCCEEDED
    assert released.deployment_state is DeploymentState.HEALTHY

    staging_v2 = _execution_spec(
        operation_id="stage-linux-v2",
        platform=Platform.LINUX,
        environment_id="staging-linux",
        tier=EnvironmentTier.STAGING,
        release_sha=RELEASE_V2,
        artifact_digest=ARTIFACT_V2,
        audience="provider-staging",
        scope="deploy:staging",
    )
    execution.submit(staging_v2, now=NOW + timedelta(minutes=2))
    execution.prepare(staging_v2.operation_id, now=NOW + timedelta(minutes=2))
    assert execution.complete(staging_v2.operation_id, now=NOW + timedelta(minutes=2)).state is OperationState.SUCCEEDED

    production_v2 = _execution_spec(
        operation_id="production-v2",
        platform=Platform.LINUX,
        environment_id="production",
        tier=EnvironmentTier.PRODUCTION,
        release_sha=RELEASE_V2,
        artifact_digest=ARTIFACT_V2,
        audience="provider-production",
        scope="deploy:production",
    )
    provider.unhealthy_intents.add(production_v2.intent.intent_id)
    execution.submit(production_v2, now=NOW + timedelta(minutes=3))
    execution.prepare(production_v2.operation_id, now=NOW + timedelta(minutes=3))
    rolled_back = execution.complete(
        production_v2.operation_id,
        now=NOW + timedelta(minutes=3),
    )
    assert rolled_back.state is OperationState.ROLLED_BACK
    assert rolled_back.deployment_state is DeploymentState.ROLLED_BACK
    assert provider.rollback_calls == [production_v2.intent.intent_id]

    maintenance_port = FakeOperationsPort()
    operations = ProductOperationsCoordinator(PROJECT_ID, maintenance_port)
    service = DeployableService(
        "service-api",
        PROJECT_ID,
        "production",
        RELEASE_V2,
        0,
        (
            ServiceReplica("replica-linux", "linux-b"),
            ServiceReplica("replica-windows", "windows-a"),
        ),
        min_healthy_replicas=2,
        credential_refs=(SECRET_REF,),
    )
    operations.register(service)
    failed_observation = ServiceObservation(
        "service-api",
        RELEASE_V2,
        (),
        ("replica-linux", "replica-windows"),
        ("operations:health:release-v2-failed",),
        NOW + timedelta(minutes=4),
    )
    failed_service = operations.record_observation(failed_observation)
    assert failed_service.health is ServiceHealth.ROLLBACK_REQUIRED

    incidents = IncidentRepairReleaseCoordinator(PROJECT_ID)
    trigger = IncidentTrigger(
        PROJECT_ID,
        "service-api",
        "production",
        RELEASE_V2,
        IncidentKind.HEALTH,
        IncidentSeverity.HIGH,
        failed_observation.evidence_refs,
        "approval:incident:c5",
        NOW + timedelta(minutes=4),
    )
    incident = incidents.open_incident("incident-c5", trigger, operations.snapshot())
    assert incident.state is IncidentState.OPEN

    rollback_observation = RollbackObservation(
        "service-api",
        RELEASE_V2,
        RELEASE_V1,
        True,
        (f"provider:rollback:{production_v2.intent.intent_id}",),
        NOW + timedelta(minutes=5),
    )
    restored_service = operations.record_rollback(rollback_observation)
    assert restored_service.health is ServiceHealth.ROLLED_BACK

    maintenance_request = MaintenanceRequest(
        "maintenance-c5-1",
        "service-api",
        MaintenanceAction.RESTART,
        "verify service after rollback",
        ("incident:incident-c5", rollback_observation.evidence_refs[0]),
        approval_ref="approval:maintenance:c5",
    )
    first_maintenance = operations.request_maintenance(maintenance_request)
    assert first_maintenance.result.applied
    assert operations.request_maintenance(maintenance_request) == first_maintenance
    assert maintenance_port.apply_calls == [maintenance_request.request_id]

    operations_snapshot = operations.snapshot()
    incident_snapshot = incidents.snapshot()

    final_store = SQLiteStore(db)
    final_store.initialize()
    final_projects = ProductProjectRepository(final_store)
    final_project = final_projects.get(PROJECT_ID)
    final_binding = ProductProjectCoordinatorBinding(final_project, graph)
    final_coordinator = ProductFactoryCheckpointHost(final_store).restore_latest(
        host_task_id=v2_task.task_id,
        binding=final_binding,
    )
    final_operations = ProductOperationsCoordinator(PROJECT_ID, maintenance_port)
    final_operations.restore(operations_snapshot)
    final_incidents = IncidentRepairReleaseCoordinator(PROJECT_ID)
    final_incidents.restore(incident_snapshot)

    history = final_projects.spec_history(PROJECT_ID)
    assert [revision.spec_version for revision in history] == [1, 2]
    assert final_project.spec_version == 2
    assert final_project.spec.supersedes_spec_version == 1
    assert final_coordinator.snapshot().project_id == PROJECT_ID
    assert final_operations.health_summary().release_ready
    assert final_incidents.get("incident-c5").trigger.project_id == PROJECT_ID
    assert final_operations.request_maintenance(maintenance_request) == first_maintenance
    assert maintenance_port.apply_calls == [maintenance_request.request_id]

    with final_store.connection() as conn:
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE stage=?",
            ("product_factory.coordinator.v1",),
        ).fetchone()[0]
    assert checkpoint_count >= 7
    assert len(provider.deploy_calls) == len(set(provider.deploy_calls))
    assert set(provider.deploy_calls) == {
        linux_stage.intent.intent_id,
        windows_stage.intent.intent_id,
        production_v1.intent.intent_id,
        staging_v2.intent.intent_id,
        production_v2.intent.intent_id,
    }
    assert all(
        spec.intent.project_id == PROJECT_ID
        for spec in (linux_stage, windows_stage, production_v1, staging_v2, production_v2)
    )
    assert service.project_id == PROJECT_ID
    assert trigger.project_id == PROJECT_ID
