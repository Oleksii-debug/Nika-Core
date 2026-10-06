from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_deployment_handoff import (
    BuildDeploymentAuthority,
    BuildDeploymentHandoff,
    BuildDeploymentHandoffError,
)
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionResult,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
    BuildOutputPolicy,
    DurableBuildExecutionHost,
    SQLiteBuildExecutionCheckpointStore,
)
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_deployment import (
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
    ResourceEnvelope,
    RollbackEvidence,
)
from nika_core.product_factory_deployment_checkpoint import (
    DurableDeploymentFabric,
    ProductFactoryDeploymentCheckpointHost,
)
from nika_core.toolsmith.contracts import AllowedPathPolicy, ChangedFile

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
SOURCE_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64


@dataclass
class Available:
    def is_available(self, node_id: str) -> bool:
        return True


@dataclass
class BuildAuthorityPort:
    value: ProjectExecutionAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


@dataclass
class OutputPolicyPort:
    value: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


@dataclass
class FileEvidence:
    def collect(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> tuple[ChangedFile, ...]:
        return ()


@dataclass
class BuildNodePort:
    result: BuildExecutionResult
    run_calls: int = 0
    inspect_calls: int = 0

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        return self.result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        self.inspect_calls += 1
        return self.result


@dataclass
class HandoffAuthorityPort:
    value: BuildDeploymentAuthority
    calls: int = 0

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        self.calls += 1
        return self.value


class HealthyProvider:
    def __init__(self) -> None:
        self.deploy_calls = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        return ProviderDeploymentResult(True, False, ("deploy:ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health:ok",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError("healthy staging deployment must not roll back")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect:healthy",),
            release=intent.release,
        )


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity("local-build-1", Platform.WINDOWS, "x86_64", "local-instance-1"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(8, 16384, 65536),
    )


def _execution_authority() -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        "work-1",
        frozenset({"build_release"}),
        ("local-build-1",),
        ("products",),
        (),
        (),
        (ApprovedBuildCommand("build", ("python.exe", "-m", "build")),),
        ("authority://trusted-build-plan",),
    )


def _build_spec() -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project-1",
            "work-1",
            Platform.WINDOWS,
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(2, 2048, 4096),
        ),
        SOURCE_SHA,
        BuildExecutionScopeRequest(
            "repo-main",
            "products/build",
            ("local-build-1",),
            (),
            (),
            "build",
        ),
        120,
    )


def _build_result(
    *,
    succeeded: bool = True,
    uncertain: bool = False,
    source_sha: str = SOURCE_SHA,
) -> BuildExecutionResult:
    return BuildExecutionResult(
        source_sha,
        ARTIFACT_DIGEST,
        succeeded,
        uncertain,
        ("evidence://local-build-receipt",),
        NOW,
    )


def _handoff_authority(
    *,
    repository_id: str = "repo-main",
    release_version: str = "1.0.0",
    tier: EnvironmentTier = EnvironmentTier.STAGING,
) -> BuildDeploymentAuthority:
    return BuildDeploymentAuthority(
        project_id="project-1",
        repository_id=repository_id,
        work_id="work-1",
        release_version=release_version,
        staging_environment=EnvironmentIdentity(
            "staging-local",
            "project-1",
            tier,
            "local-staging-provider",
        ),
        migration_refs=(),
    )


def _setup(tmp_path, *, build_result: BuildExecutionResult | None = None):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )

    registry = ExecutionNodeRegistry()
    registry.register(_node())
    coordinator = BuildExecutionCoordinator(
        registry,
        Available(),
        BuildAuthorityPort(_execution_authority()),
    )
    build_port = BuildNodePort(build_result or _build_result())
    build_host = DurableBuildExecutionHost(
        coordinator,
        build_port,
        FileEvidence(),
        OutputPolicyPort(
            BuildOutputPolicy(
                "project-1",
                "repo-main",
                "work-1",
                AllowedPathPolicy(("products/build",)),
                8,
                RepositoryPathIdentity.CASE_INSENSITIVE,
            )
        ),
        SQLiteBuildExecutionCheckpointStore(
            store,
            task.task_id,
            "project-1",
        ),
    )

    provider = HealthyProvider()
    deployment = DurableDeploymentFabric(
        provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(store),
        host_task_id=task.task_id,
        project_id="project-1",
    )
    authority = HandoffAuthorityPort(_handoff_authority())
    handoff = BuildDeploymentHandoff(build_host, deployment, authority)
    return build_host, provider, authority, handoff


def _finish_build(host: DurableBuildExecutionHost):
    spec = _build_spec()
    host.submit(spec, now=NOW)
    host.prepare(spec.request.work_id, now=NOW)
    host.begin_dispatch(spec.request.work_id, now=NOW)
    return host.execute(spec.request.work_id, now=NOW)


def test_successful_build_enters_exact_staging_release_once(tmp_path) -> None:
    build_host, provider, _, handoff = _setup(tmp_path)
    completed = _finish_build(build_host)
    assert completed.evidence is not None

    deployed = handoff.deploy_staging("work-1")
    duplicate = handoff.deploy_staging("work-1")

    assert deployed.state is DeploymentState.HEALTHY
    assert deployed.intent.environment.tier is EnvironmentTier.STAGING
    assert deployed.intent.release.project_id == "project-1"
    assert deployed.intent.release.version == "1.0.0"
    assert deployed.intent.release.source_sha == SOURCE_SHA
    assert deployed.intent.release.artifact_digest == ARTIFACT_DIGEST
    assert duplicate == deployed
    assert provider.deploy_calls == 1


def test_nonterminal_build_is_rejected_before_deployment_effect(tmp_path) -> None:
    build_host, provider, _, handoff = _setup(tmp_path)
    spec = _build_spec()
    build_host.submit(spec, now=NOW)
    build_host.prepare("work-1", now=NOW)
    build_host.begin_dispatch("work-1", now=NOW)

    with pytest.raises(BuildDeploymentHandoffError, match="not durably successful"):
        handoff.deploy_staging("work-1")

    assert provider.deploy_calls == 0


@pytest.mark.parametrize(
    ("build_result", "expected_state"),
    [
        (_build_result(succeeded=False), "failed"),
        (_build_result(succeeded=False, uncertain=True), "reconcile_required"),
        (_build_result(source_sha="c" * 40), "reconcile_required"),
    ],
)
def test_failed_uncertain_or_mismatched_build_never_deploys(
    tmp_path,
    build_result: BuildExecutionResult,
    expected_state: str,
) -> None:
    build_host, provider, _, handoff = _setup(
        tmp_path,
        build_result=build_result,
    )
    completed = _finish_build(build_host)
    assert completed.state.value == expected_state

    with pytest.raises(BuildDeploymentHandoffError, match="not durably successful"):
        handoff.deploy_staging("work-1")

    assert provider.deploy_calls == 0


def test_mismatched_handoff_authority_is_rejected_before_effect(tmp_path) -> None:
    build_host, provider, authority, handoff = _setup(tmp_path)
    _finish_build(build_host)
    authority.value = _handoff_authority(repository_id="repo-other")

    with pytest.raises(BuildDeploymentHandoffError, match="wrong build identity"):
        handoff.deploy_staging("work-1")

    assert provider.deploy_calls == 0


def test_handoff_authority_cannot_target_production() -> None:
    with pytest.raises(BuildDeploymentHandoffError, match="must target staging"):
        _handoff_authority(tier=EnvironmentTier.PRODUCTION)


def test_mutated_staging_environment_is_readmitted_before_effect(tmp_path) -> None:
    build_host, provider, authority, handoff = _setup(tmp_path)
    _finish_build(build_host)
    object.__setattr__(
        authority.value.staging_environment,
        "tier",
        EnvironmentTier.PRODUCTION,
    )

    with pytest.raises(BuildDeploymentHandoffError, match="must target staging"):
        handoff.deploy_staging("work-1")

    assert provider.deploy_calls == 0


def test_corrupt_build_evidence_is_rejected_before_effect(tmp_path) -> None:
    build_host, provider, _, handoff = _setup(tmp_path)
    completed = _finish_build(build_host)
    assert completed.evidence is not None
    object.__setattr__(completed.evidence, "artifact_digest", "not-a-digest")

    with pytest.raises(BuildDeploymentHandoffError, match="failed readmission"):
        handoff.deploy_staging("work-1")

    assert provider.deploy_calls == 0


def test_authority_drift_cannot_redispatch_same_finished_build(tmp_path) -> None:
    build_host, provider, authority, handoff = _setup(tmp_path)
    _finish_build(build_host)

    first = handoff.deploy_staging("work-1")
    authority.value = _handoff_authority(release_version="1.0.1")

    with pytest.raises(ValueError, match="intent id conflicts"):
        handoff.deploy_staging("work-1")

    assert first.state is DeploymentState.HEALTHY
    assert provider.deploy_calls == 1


def test_behavioral_authority_subclass_is_not_admitted(tmp_path) -> None:
    build_host, provider, authority, handoff = _setup(tmp_path)
    _finish_build(build_host)

    class ForgedAuthority(BuildDeploymentAuthority):
        pass

    authority.value = ForgedAuthority(
        "project-1",
        "repo-main",
        "work-1",
        "1.0.0",
        EnvironmentIdentity(
            "staging-local",
            "project-1",
            EnvironmentTier.STAGING,
            "local-staging-provider",
        ),
    )

    with pytest.raises(BuildDeploymentHandoffError, match="invalid carrier"):
        handoff.deploy_staging("work-1")

    assert provider.deploy_calls == 0
