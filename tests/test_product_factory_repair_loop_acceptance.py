from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerAdapterError,
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
)
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkState,
)
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    NormalizedBuildEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
)
from nika_core.product_factory_incident_contracts import (
    IncidentKind,
    IncidentSeverity,
    IncidentState,
    IncidentTrigger,
    ProductIncidentError,
    ReleaseDisposition,
    ReleaseEvidence,
    RepairCandidateEvidence,
    RepairWorkOrder,
)
from nika_core.product_factory_incident_persistence import (
    dump_incident_snapshot,
    load_incident_snapshot,
)
from nika_core.product_factory_incidents import (
    IncidentRepairReleaseCoordinator,
    TrustedReviewAuthority,
)
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
from nika_core.product_factory_repair_release_integration import (
    ProductRepairReleaseIntegrationError,
    release_ref_from_repair_build,
)
from nika_core.product_factory_toolsmith_integration import ProductFactoryToolsmithBridge
from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    ArtifactEvidence,
    CandidateState,
    CapabilityManifestV1,
    ChangedFile,
    CodingJob,
    CodingResult,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.repository import ToolsmithRepository
from nika_core.toolsmith.service import CapabilityEscalationService

PROJECT = "project-a"
SERVICE = "api"
ENVIRONMENT = "prod-eu"
REPOSITORY = "repo-a"
COMPONENT = "component-api"
CAPABILITY = "safe-config-repair"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40
OLD_ARTIFACT = "4" * 64
ARTIFACT = "5" * 64
DIFF = "6" * 64
TEST_DIGEST = "7" * 64
CAPABILITY_DIGEST = "8" * 64
FILE_DIGEST = "9" * 64
NOW = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
PERMISSIONS = frozenset({"repo.read", "repo.write", "tests.run"})
GOAL = "Repair the degraded API without widening component ownership."
COMMAND = ("python", "-m", "pytest", "tests/test_api.py")


def _run(awaitable):
    return asyncio.run(awaitable)


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT,
        repositories=(RepositoryRef(REPOSITORY, "github", "example/product", "main"),),
        components=(
            ProductComponent(
                COMPONENT,
                REPOSITORY,
                ("src/api.py", "tests/test_api.py"),
                test_commands=(COMMAND,),
            ),
        ),
    )


def _operations() -> ProductOperationsCoordinator:
    operations = ProductOperationsCoordinator(PROJECT)
    operations.register(
        DeployableService(
            SERVICE,
            PROJECT,
            ENVIRONMENT,
            OLD_SHA,
            0,
            (ServiceReplica("api-r1", "node-a"), ServiceReplica("api-r2", "node-b")),
        )
    )
    operations.record_observation(
        ServiceObservation(
            SERVICE,
            OLD_SHA,
            ("api-r1", "api-r2"),
            (),
            ("health://api/healthy",),
            NOW,
        )
    )
    operations.record_observation(
        ServiceObservation(
            SERVICE,
            OLD_SHA,
            ("api-r1",),
            ("api-r2",),
            ("health://api/degraded",),
            NOW + timedelta(minutes=1),
        )
    )
    return operations


def _trigger() -> IncidentTrigger:
    return IncidentTrigger(
        PROJECT,
        SERVICE,
        ENVIRONMENT,
        OLD_SHA,
        IncidentKind.HEALTH,
        IncidentSeverity.HIGH,
        ("health://api/degraded",),
        "approval://incident/repair-loop",
        NOW + timedelta(minutes=1),
    )


def _work_order(request) -> RepairWorkOrder:
    return RepairWorkOrder(
        request.work_id,
        "incident-1",
        PROJECT,
        SERVICE,
        REPOSITORY,
        COMPONENT,
        OLD_SHA,
        request.goal,
        request.allowed_paths,
        request.permission_ceiling,
        request.acceptance_commands,
        ("health://api/degraded",),
        NOW + timedelta(minutes=2),
    )


class _ProductContext:
    async def context_for(self, _request):
        return CodingWorkerDispatchContext(
            repository_tree_digest="tree://repair-loop",
            lease=WorkspaceLease(
                "lease-product-repair",
                Path("sandbox/product-repair"),
                IsolationClass.PROCESS_CONTAINED,
                "2026-08-24T00:00:00Z",
            ),
            process_policy=ProcessPolicy(("python",)),
            network_policy=NetworkPolicy(),
            resource_budget=ResourceBudget(120, 100_000, 4),
        )


class _ProductEvidence:
    async def collect(self, request, _job, _result):
        return CodingWorkerExecutionEvidence(
            request.work_id,
            request.repository_id,
            request.base_sha,
            NEW_SHA,
            DIFF,
        )


@dataclass
class _RegisteredCapabilityProductWorker:
    escalation: CapabilityEscalationService
    wrong_path: bool = False
    fail: bool = False
    execute_calls: int = 0

    async def execute(self, job: CodingJob) -> CodingResult:
        self.execute_calls += 1
        if self.fail:
            return CodingResult(
                job_id=job.job_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "deterministic local worker failure",
                    retryable=True,
                ),
            )
        registered = self.escalation.reconcile_resume(
            task_id=job.task_id,
            capability_id=CAPABILITY,
        )
        if registered is None:
            raise AssertionError("product repair ran before exact Toolsmith registration")
        changed_path = "outside/attack.py" if self.wrong_path else "src/api.py"
        return CodingResult(
            job_id=job.job_id,
            changed_files=(ChangedFile(changed_path, FILE_DIGEST, 42),),
            test_evidence=(TestEvidence(COMMAND, 0, TEST_DIGEST),),
            artifacts=(ArtifactEvidence("repair-package", ARTIFACT, "application/zip"),),
        )

    async def cancel(self, _job_id: str) -> None:
        return None

    async def inspect(self, _job_id: str) -> RecoveryState | None:
        return None

    async def recover(self, job: CodingJob, _state: RecoveryState) -> CodingResult:
        return await self.execute(job)


@dataclass
class _ToolsmithWorker:
    crash_once: bool = False
    recovery_state: RecoveryState | None = None
    execute_calls: int = 0
    recover_calls: int = 0

    async def execute(self, job: CodingJob) -> CodingResult:
        self.execute_calls += 1
        if self.crash_once:
            self.crash_once = False
            self.recovery_state = RecoveryState("candidate-written", "resume-token")
            raise OSError("simulated crash after isolated Toolsmith side effect")
        return self._success(job)

    async def cancel(self, _job_id: str) -> None:
        return None

    async def inspect(self, _job_id: str) -> RecoveryState | None:
        return self.recovery_state

    async def recover(self, job: CodingJob, _state: RecoveryState) -> CodingResult:
        self.recover_calls += 1
        return self._success(job)

    @staticmethod
    def _success(job: CodingJob) -> CodingResult:
        command = job.acceptance_commands[0].argv
        return CodingResult(
            job_id=job.job_id,
            changed_files=(
                ChangedFile("toolsmith/generated/safe_config_repair.py", FILE_DIGEST, 80),
            ),
            test_evidence=(TestEvidence(command, 0, TEST_DIGEST),),
            artifacts=(
                ArtifactEvidence("capability", CAPABILITY_DIGEST, "application/python"),
            ),
        )


def _toolsmith_job(tmp_path, gap) -> CodingJob:
    return CodingJob(
        job_id="toolsmith-job-1",
        task_id=gap.task_id,
        goal="Build the missing bounded config-repair capability.",
        repository=RepositorySnapshot(REPOSITORY, OLD_SHA, "tree://toolsmith"),
        lease=WorkspaceLease(
            "lease-toolsmith",
            tmp_path / "toolsmith-sandbox",
            IsolationClass.PROCESS_CONTAINED,
            "2026-08-24T00:00:00Z",
        ),
        allowed_paths=AllowedPathPolicy(("toolsmith/generated",)),
        process_policy=ProcessPolicy(("python",)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(120, 100_000, 4),
        acceptance_commands=(AcceptanceCommand(COMMAND),),
        permission_ceiling=PERMISSIONS,
    )


def _register_capability(service: CapabilityEscalationService, checkpoint, tmp_path) -> None:
    version, selected = service.choose_reuse(
        gap=checkpoint.gap,
        candidates=(),
        expected_version=checkpoint.row_version,
    )
    assert selected is None
    version, result = _run(
        service.build(
            gap=checkpoint.gap,
            job=_toolsmith_job(tmp_path, checkpoint.gap),
            expected_version=version,
        )
    )
    assert result.succeeded
    version = service.start_verification(gap=checkpoint.gap, expected_version=version)
    version = service.accept_verification(
        gap=checkpoint.gap,
        expected_version=version,
        candidate_digest=CAPABILITY_DIGEST,
        verifier_evidence={"review_ref": "review://toolsmith/independent"},
    )
    service.register(
        gap=checkpoint.gap,
        expected_version=version,
        manifest=CapabilityManifestV1(
            CAPABILITY,
            "1.0.0",
            CAPABILITY_DIGEST,
            "toolsmith.generated.safe_config_repair:run",
            frozenset({"repo.read", "repo.write", "tests.run"}),
            "local://toolsmith/acceptance",
        ),
    )


@dataclass
class _DeploymentProvider:
    unhealthy: set[str] = field(default_factory=set)
    uncertain: set[str] = field(default_factory=set)
    rollback_success: bool = True
    wrong_health_release: set[str] = field(default_factory=set)
    inspections: dict[str, ProviderInspection] = field(default_factory=dict)
    deploy_calls: dict[str, int] = field(default_factory=dict)

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls[intent.intent_id] = self.deploy_calls.get(intent.intent_id, 0) + 1
        if intent.intent_id in self.uncertain:
            return ProviderDeploymentResult(False, True, (f"deploy://{intent.intent_id}/unknown",))
        return ProviderDeploymentResult(True, False, (f"deploy://{intent.intent_id}/applied",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        release_sha = OTHER_SHA if intent.intent_id in self.wrong_health_release else intent.release.source_sha
        return HealthEvidence(
            intent.environment.environment_id,
            release_sha,
            intent.intent_id not in self.unhealthy,
            (f"health://{intent.intent_id}",),
            NOW + timedelta(minutes=5),
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha if self.rollback_success else None,
            self.rollback_success,
            (f"rollback://{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return self.inspections.get(
            intent.intent_id,
            ProviderInspection(
                intent.release.source_sha,
                True,
                (f"inspect://{intent.intent_id}",),
            ),
        )


def _intent(
    intent_id: str,
    tier: EnvironmentTier,
    release: ReleaseRef,
    *,
    environment_id: str | None = None,
) -> DeploymentIntent:
    env_id = environment_id or ("stage-eu" if tier is EnvironmentTier.STAGING else ENVIRONMENT)
    return DeploymentIntent(
        intent_id,
        PROJECT,
        EnvironmentIdentity(env_id, PROJECT, tier, "provider://local-fake"),
        release,
    )


def _seed_known_good(fabric: DeploymentFabric) -> None:
    old = ReleaseRef(PROJECT, "1.0.0", OLD_SHA, OLD_ARTIFACT)
    fabric.deploy(_intent("old-stage", EnvironmentTier.STAGING, old))
    fabric.deploy(_intent("old-prod", EnvironmentTier.PRODUCTION, old))


def test_complete_repair_loop_uses_real_toolsmith_state_and_survives_restart(tmp_path) -> None:
    operations = _operations()
    incidents = IncidentRepairReleaseCoordinator(PROJECT)
    opened = incidents.open_incident("incident-1", _trigger(), operations.snapshot())
    assert opened.state is IncidentState.OPEN
    assert incidents.open_incident("incident-retry", _trigger(), operations.snapshot()) == opened

    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={REPOSITORY: OLD_SHA},
        goals={COMPONENT: GOAL},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.ready_requests()[0]
    work = _work_order(request)
    incidents.create_repair_work_order(work)

    store = SQLiteStore(tmp_path / "toolsmith.db")
    store.initialize()
    repository = ToolsmithRepository(store)
    toolsmith_worker = _ToolsmithWorker()
    toolsmith = CapabilityEscalationService(
        repository=repository,
        checkpoints=CheckpointService(store),
        worker=toolsmith_worker,
    )
    product_worker = _RegisteredCapabilityProductWorker(toolsmith)
    adapter = CodingWorkerComponentAdapter(product_worker, _ProductContext(), _ProductEvidence())
    bridge = ProductFactoryToolsmithBridge(toolsmith, adapter)

    gap = bridge.begin_gap(
        request,
        capability_id=CAPABILITY,
        reason="repair requires a bounded config-edit capability",
        attempted_methods=("registry-search", "reuse-catalog-search"),
    )
    assert gap.row_version == 0
    assert gap.state is CandidateState.PROPOSED
    _register_capability(toolsmith, gap, tmp_path)
    assert toolsmith.reconcile_resume(task_id=gap.task_id, capability_id=CAPABILITY) == {
        "task_id": gap.task_id,
        "capability_id": CAPABILITY,
        "version": "1.0.0",
        "digest": CAPABILITY_DIGEST,
    }

    outcome = _run(adapter.run_component_outcome(coordinator, COMPONENT))
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    stale_authority = TrustedReviewAuthority(
        coordinator.snapshot(), coordinator.trusted_plan_fingerprint
    )
    accepted = coordinator.review(
        COMPONENT,
        ReviewDecision(
            "independent-reviewer",
            True,
            "bounded repair passes independent review",
            ("review://repair/accepted",),
        ),
    )
    assert accepted.result is not None
    candidate = RepairCandidateEvidence(
        "candidate-1",
        "incident-1",
        work.work_order_id,
        OLD_SHA,
        accepted.result.result_sha,
        ARTIFACT,
        accepted.result.diff_digest,
        (TEST_DIGEST,),
        (f"toolsmith://{CAPABILITY}@1.0.0/{CAPABILITY_DIGEST}",),
        "review://repair/accepted",
        True,
        NOW + timedelta(minutes=3),
    )
    with pytest.raises(ProductIncidentError, match="review|worker result"):
        incidents.record_candidate(candidate, stale_authority)

    review_authority = TrustedReviewAuthority(
        coordinator.snapshot(), coordinator.trusted_plan_fingerprint
    )
    assert (
        incidents.record_candidate(candidate, review_authority).state
        is IncidentState.RELEASE_READY
    )

    build = NormalizedBuildEvidence(
        work.work_order_id,
        "local-build-node",
        NEW_SHA,
        ARTIFACT,
        True,
        ("build://local/exact-reviewed-repair",),
    )
    release_ref = release_ref_from_repair_build(
        work_order=work,
        candidate=candidate,
        build=build,
        version="1.0.1-repair.1",
    )
    assert release_ref.source_sha == NEW_SHA
    assert release_ref.artifact_digest == ARTIFACT

    provider = _DeploymentProvider()
    fabric = DeploymentFabric(provider)
    _seed_known_good(fabric)
    staging_intent = _intent("repair-stage", EnvironmentTier.STAGING, release_ref)
    production_intent = _intent("repair-prod", EnvironmentTier.PRODUCTION, release_ref)
    staging = fabric.deploy(staging_intent)
    production = fabric.deploy(production_intent)
    assert staging.state is DeploymentState.HEALTHY
    assert production.state is DeploymentState.HEALTHY
    assert production.previous_release_sha == OLD_SHA
    duplicate = fabric.deploy(staging_intent)
    assert duplicate == staging
    assert provider.deploy_calls[staging_intent.intent_id] == 1

    assert production.health is not None
    release = ReleaseEvidence(
        "release-event-1",
        "incident-1",
        candidate.candidate_id,
        OLD_SHA,
        NEW_SHA,
        ARTIFACT,
        staging_intent.intent_id,
        production_intent.intent_id,
        ReleaseDisposition.HEALTHY,
        staging.provider_evidence_refs + production.provider_evidence_refs,
        production.health.evidence_refs,
        None,
        None,
        NOW + timedelta(minutes=6),
    )
    closed = incidents.record_release(release, fabric.snapshot())
    assert closed.state is IncidentState.RESOLVED

    incident_bytes = dump_incident_snapshot(incidents.snapshot())
    restored_snapshot = load_incident_snapshot(
        incident_bytes,
        deployments=fabric.snapshot(),
        review_authorities=(review_authority,),
    )
    restarted_incidents = IncidentRepairReleaseCoordinator(PROJECT)
    restarted_incidents.restore(
        restored_snapshot,
        fabric.snapshot(),
        (review_authority,),
    )
    assert restarted_incidents.get("incident-1").state is IncidentState.RESOLVED

    restarted_coordinator = ProductFactoryCoordinator(_graph())
    restarted_coordinator.restore(
        coordinator.snapshot(),
        trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
    )
    assert restarted_coordinator.snapshot() == coordinator.snapshot()

    restarted_fabric = DeploymentFabric(provider)
    restarted_fabric.restore(fabric.snapshot())
    assert restarted_fabric.deploy(staging_intent) == staging
    assert provider.deploy_calls[staging_intent.intent_id] == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_toolsmith = CapabilityEscalationService(
        repository=ToolsmithRepository(restarted_store),
        checkpoints=CheckpointService(restarted_store),
        worker=_ToolsmithWorker(),
    )
    assert restarted_toolsmith.reconcile_resume(
        task_id=gap.task_id,
        capability_id=CAPABILITY,
    )["digest"] == CAPABILITY_DIGEST


def test_build_lineage_rejects_failed_stale_or_different_artifact() -> None:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={REPOSITORY: OLD_SHA},
        goals={COMPONENT: GOAL},
        permission_ceiling=PERMISSIONS,
    )
    work = _work_order(coordinator.ready_requests()[0])
    candidate = RepairCandidateEvidence(
        "candidate-1",
        "incident-1",
        work.work_order_id,
        OLD_SHA,
        NEW_SHA,
        ARTIFACT,
        DIFF,
        (TEST_DIGEST,),
        ("provenance://worker",),
        "review://accepted",
        True,
        NOW + timedelta(minutes=3),
    )
    build = NormalizedBuildEvidence(
        work.work_order_id,
        "local-build-node",
        NEW_SHA,
        ARTIFACT,
        True,
        ("build://exact",),
    )

    for bad, message in (
        (replace(build, succeeded=False), "did not succeed"),
        (replace(build, release_sha=OTHER_SHA), "SHA"),
        (replace(build, artifact_digest="a" * 64), "artifact"),
        (replace(build, work_id="other-work"), "another repair work order"),
    ):
        with pytest.raises(ProductRepairReleaseIntegrationError, match=message):
            release_ref_from_repair_build(
                work_order=work,
                candidate=candidate,
                build=bad,
                version="1.0.1",
            )


def test_worker_wrong_file_and_worker_failure_fail_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "toolsmith.db")
    store.initialize()
    toolsmith = CapabilityEscalationService(
        repository=ToolsmithRepository(store),
        checkpoints=CheckpointService(store),
        worker=_ToolsmithWorker(),
    )

    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={REPOSITORY: OLD_SHA},
        goals={COMPONENT: GOAL},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.ready_requests()[0]
    bridge = ProductFactoryToolsmithBridge(
        toolsmith,
        CodingWorkerComponentAdapter(
            _RegisteredCapabilityProductWorker(toolsmith), _ProductContext(), _ProductEvidence()
        ),
    )
    gap = bridge.begin_gap(
        request,
        capability_id=CAPABILITY,
        reason="bounded capability required",
        attempted_methods=("registry-search",),
    )
    _register_capability(toolsmith, gap, tmp_path)

    wrong = CodingWorkerComponentAdapter(
        _RegisteredCapabilityProductWorker(toolsmith, wrong_path=True),
        _ProductContext(),
        _ProductEvidence(),
    )
    with pytest.raises(CodingWorkerAdapterError, match="outside component allowed paths"):
        _run(wrong.dispatch(request))
    assert coordinator.snapshot().records[0].state is WorkState.READY

    failing = CodingWorkerComponentAdapter(
        _RegisteredCapabilityProductWorker(toolsmith, fail=True),
        _ProductContext(),
        _ProductEvidence(),
    )
    failed = _run(failing.run_component_outcome(coordinator, COMPONENT))
    assert failed.record.state is WorkState.REPAIR_REQUIRED
    assert failed.failure is not None and failed.failure.retryable


def test_toolsmith_build_crash_restarts_from_durable_building_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "toolsmith.db")
    store.initialize()
    worker = _ToolsmithWorker(crash_once=True)
    service = CapabilityEscalationService(
        repository=ToolsmithRepository(store),
        checkpoints=CheckpointService(store),
        worker=worker,
    )

    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={REPOSITORY: OLD_SHA},
        goals={COMPONENT: GOAL},
        permission_ceiling=PERMISSIONS,
    )
    adapter = CodingWorkerComponentAdapter(
        _RegisteredCapabilityProductWorker(service), _ProductContext(), _ProductEvidence()
    )
    gap = ProductFactoryToolsmithBridge(service, adapter).begin_gap(
        coordinator.ready_requests()[0],
        capability_id=CAPABILITY,
        reason="bounded capability required",
        attempted_methods=("registry-search",),
    )
    version, _ = service.choose_reuse(
        gap=gap.gap,
        candidates=(),
        expected_version=gap.row_version,
    )
    job = _toolsmith_job(tmp_path, gap.gap)
    with pytest.raises(OSError, match="simulated crash"):
        _run(service.build(gap=gap.gap, job=job, expected_version=version))

    row = ToolsmithRepository(store).get_escalation(
        task_id=gap.task_id,
        capability_id=CAPABILITY,
    )
    assert row is not None
    assert row["state"] == CandidateState.BUILDING.value
    building_version = int(row["row_version"])

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    recovery_worker = _ToolsmithWorker(recovery_state=worker.recovery_state)
    restarted = CapabilityEscalationService(
        repository=ToolsmithRepository(restarted_store),
        checkpoints=CheckpointService(restarted_store),
        worker=recovery_worker,
    )
    version, result = _run(
        restarted.recover_build(
            gap=gap.gap,
            job=job,
            expected_version=building_version,
        )
    )
    assert result.succeeded
    assert recovery_worker.execute_calls == 0
    assert recovery_worker.recover_calls == 1
    assert version == building_version + 1


def test_deployment_uncertainty_restarts_and_reconciles_without_duplicate_apply() -> None:
    provider = _DeploymentProvider(uncertain={"repair-stage"})
    provider.inspections["repair-stage"] = ProviderInspection(
        NEW_SHA,
        True,
        ("inspect://repair-stage/exact",),
    )
    fabric = DeploymentFabric(provider)
    release = ReleaseRef(PROJECT, "1.0.1", NEW_SHA, ARTIFACT)
    intent = _intent("repair-stage", EnvironmentTier.STAGING, release)

    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN
    restarted = DeploymentFabric(provider)
    restarted.restore(fabric.snapshot())
    reconciled = restarted.reconcile(intent.intent_id)
    assert reconciled.state is DeploymentState.HEALTHY
    assert provider.deploy_calls[intent.intent_id] == 1


def test_health_identity_mismatch_and_rollback_uncertainty_fail_closed() -> None:
    release = ReleaseRef(PROJECT, "1.0.1", NEW_SHA, ARTIFACT)

    wrong_health_provider = _DeploymentProvider(wrong_health_release={"repair-stage"})
    wrong_health = DeploymentFabric(wrong_health_provider)
    with pytest.raises(DeploymentFabricError, match="health evidence release mismatch"):
        wrong_health.deploy(_intent("repair-stage", EnvironmentTier.STAGING, release))

    provider = _DeploymentProvider(unhealthy={"repair-prod"}, rollback_success=False)
    fabric = DeploymentFabric(provider)
    _seed_known_good(fabric)
    fabric.deploy(_intent("repair-stage", EnvironmentTier.STAGING, release))
    rejected = fabric.deploy(_intent("repair-prod", EnvironmentTier.PRODUCTION, release))
    assert rejected.state is DeploymentState.REJECTED
    snapshot = fabric.snapshot()
    restarted = DeploymentFabric(provider)
    restarted.restore(snapshot)
    assert restarted.snapshot() == snapshot

    operations = _operations()
    incidents = IncidentRepairReleaseCoordinator(PROJECT)
    incidents.open_incident("incident-1", _trigger(), operations.snapshot())
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={REPOSITORY: OLD_SHA},
        goals={COMPONENT: GOAL},
        permission_ceiling=PERMISSIONS,
    )
    incidents.create_repair_work_order(_work_order(coordinator.ready_requests()[0]))
    assert incidents.get("incident-1").state is IncidentState.PLANNED
    with pytest.raises(ProductIncidentError, match="unknown repair candidate"):
        incidents.record_release(
            ReleaseEvidence(
                "forged-terminal",
                "incident-1",
                "missing-candidate",
                OLD_SHA,
                NEW_SHA,
                ARTIFACT,
                "repair-stage",
                "repair-prod",
                ReleaseDisposition.ROLLED_BACK,
                ("deploy://repair-stage/applied", "deploy://repair-prod/applied"),
                ("health://repair-prod", "rollback://repair-prod"),
                OLD_SHA,
                None,
                NOW + timedelta(minutes=7),
            ),
            fabric.snapshot(),
        )
