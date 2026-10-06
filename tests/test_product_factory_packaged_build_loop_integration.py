from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_admission import (
    ReviewedBuildExecutionPolicy,
    reviewed_component_build_spec,
)
from nika_core.product_factory_build_deployment_handoff import (
    BuildDeploymentAuthority,
    BuildDeploymentHandoff,
)
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import BuildOutputPolicy
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
)
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNode,
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
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import (
    AllowedPathPolicy,
    CodingResult,
    ResourceBudget,
    TestEvidence,
)

PROJECT_ID = "product-build-loop-integration"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop"
BASE_SHA = "a" * 40
RESULT_SHA = "b" * 40
DIFF_DIGEST = "c" * 64
GRAPH_DIGEST = "d" * 64
NODE_ID = "packaged-local-1"
TEST_COMMAND = ("python", "-m", "pytest", "tests")
PRODUCER = "worker:builder"
REVIEWER = "worker:independent-reviewer"
NOW = datetime(2026, 10, 6, 19, 55, tzinfo=UTC)


class ReviewAuthority:
    def verify(self, subject, evidence_refs) -> bool:
        return (
            subject.project_id == PROJECT_ID
            and subject.component_id == COMPONENT_ID
            and subject.producer_actor_id == PRODUCER
            and subject.reviewer_id == REVIEWER
            and subject.accepted is True
            and evidence_refs == ("review://independent/accepted",)
        )


@dataclass
class BuildAdmissionPolicies:
    platform: Platform

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        component_id: str,
        candidate_work_id: str,
        source_sha: str,
        review_fingerprint: str,
        spec_version: int,
        row_version: int,
        graph_digest: str,
    ) -> ReviewedBuildExecutionPolicy:
        return ReviewedBuildExecutionPolicy(
            project_id=project_id,
            repository_id=repository_id,
            component_id=component_id,
            candidate_work_id=candidate_work_id,
            source_sha=source_sha,
            review_fingerprint=review_fingerprint,
            spec_version=spec_version,
            row_version=row_version,
            graph_digest=graph_digest,
            platform=self.platform,
            requested_node_ids=(NODE_ID,),
            workspace_relpath="products/build",
            required_features=frozenset({"build"}),
            required_toolchains=frozenset({"python"}),
            resources=ResourceEnvelope(1, 1024, 2048),
            network_scopes=(),
            credential_refs=(),
            command_id="build",
            lease_seconds=120,
        )


@dataclass
class ExecutionAuthority:
    value: ProjectExecutionAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        if (
            project_id != self.value.project_id
            or repository_id != self.value.repository_id
            or work_id != self.value.work_id
        ):
            raise AssertionError("unexpected PF5 execution-authority identity")
        return self.value


@dataclass
class OutputPolicies:
    value: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        if (
            project_id != self.value.project_id
            or repository_id != self.value.repository_id
            or work_id != self.value.work_id
        ):
            raise AssertionError("unexpected PF5 output-policy identity")
        return self.value


@dataclass
class HandoffAuthority:
    value: BuildDeploymentAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        if (
            project_id != self.value.project_id
            or repository_id != self.value.repository_id
            or work_id != self.value.work_id
        ):
            raise AssertionError("unexpected PF5-to-PF6 authority identity")
        return self.value


class HealthyStagingProvider:
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


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


def _path_identity() -> RepositoryPathIdentity:
    return (
        RepositoryPathIdentity.CASE_INSENSITIVE
        if os.name == "nt"
        else RepositoryPathIdentity.CASE_SENSITIVE
    )


def _git() -> Path:
    value = shutil.which("git")
    if value is None:
        pytest.skip("git is required for the packaged build-loop integration proof")
    return Path(value).resolve()


def _real_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "reviewed candidate repo"
    output = root / "products" / "build"
    output.mkdir(parents=True)
    (output / "input.txt").write_text("reviewed-source\n", encoding="utf-8")
    git = str(_git())
    for command in (
        (git, "-C", str(root), "init"),
        (git, "-C", str(root), "config", "user.email", "tests@example.invalid"),
        (git, "-C", str(root), "config", "user.name", "Nika Tests"),
        (git, "-C", str(root), "add", "."),
        (git, "-C", str(root), "commit", "-m", "reviewed candidate"),
    ):
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    source_sha = subprocess.run(
        (git, "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return root, source_sha


def _bind_repository(
    store: SQLiteStore,
    graph: ProductRepositoryGraph,
    root: Path,
) -> None:
    locator = graph.repositories[0].locator
    ProductProjectRepository(store).create(
        project_id=PROJECT_ID,
        name="PF4-PF6 packaged build-loop proof",
        spec=ProductProjectSpec(
            goal="Build the independently reviewed candidate",
            desired_outcome="Produce and stage one verified artifact",
            repository_refs=(locator,),
        ),
        idempotency_key="create-packaged-build-loop-proof",
    )
    ProductFactoryLocalRepositoryBindings(store).bind(
        project_id=PROJECT_ID,
        repository=graph.repositories[0],
        root=root,
        expected_binding_version=None,
    )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef(
                repository_id=REPOSITORY_ID,
                provider="local-git",
                locator="C:/Nika/Product",
                default_branch="main",
                case_sensitive_paths=False,
            ),
        ),
        components=(
            ProductComponent(
                component_id=COMPONENT_ID,
                repository_id=REPOSITORY_ID,
                paths=("src", "tests"),
                build_commands=(
                    (
                        "powershell.exe",
                        "-Command",
                        "Invoke-Expression $candidate_controlled_text",
                    ),
                ),
                test_commands=(TEST_COMMAND,),
                release_identity="desktop-release",
            ),
        ),
    )


def _accepted_coordinator(
    graph: ProductRepositoryGraph,
    *,
    result_sha: str = RESULT_SHA,
) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(
        graph,
        review_authority=ReviewAuthority(),
    )
    coordinator.plan(
        base_shas={REPOSITORY_ID: BASE_SHA},
        goals={COMPONENT_ID: "Build the accessible Windows desktop product"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests", "build_release"}
        ),
    )
    request = coordinator.start(COMPONENT_ID)
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=COMPONENT_ID,
            repository_id=REPOSITORY_ID,
            base_sha=BASE_SHA,
            result_sha=result_sha,
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        command=TEST_COMMAND,
                        exit_code=0,
                        output_digest="e" * 64,
                    ),
                ),
            ),
            producer_actor_id=PRODUCER,
        )
    )
    coordinator.review(
        COMPONENT_ID,
        ReviewDecision(
            reviewer_id=REVIEWER,
            accepted=True,
            reason="independent acceptance passed",
            evidence_refs=("review://independent/accepted",),
        ),
    )
    return coordinator


def test_reviewed_candidate_reaches_durable_packaged_pf5_without_plan_argv(
    tmp_path: Path,
) -> None:
    graph = _graph()
    coordinator = _accepted_coordinator(graph)
    graph_authority = RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:1",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=graph,
        dependency_edges=(),
    )
    spec = reviewed_component_build_spec(
        authority=graph_authority,
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        policies=BuildAdmissionPolicies(_platform()),
    )

    python_executable = str(Path(sys.executable).resolve())
    execution_authority = ExecutionAuthority(
        ProjectExecutionAuthority(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            permissions=frozenset({"build_release"}),
            allowed_node_ids=(NODE_ID,),
            allowed_workspace_paths=("products/build",),
            network_scopes=(),
            credential_refs=(),
            commands=(
                ApprovedBuildCommand(
                    "build",
                    (python_executable, "-m", "build"),
                ),
            ),
            evidence_refs=("authority://packaged-build/integration",),
        )
    )
    output_policies = OutputPolicies(
        BuildOutputPolicy(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            allowed_paths=AllowedPathPolicy(("products/build",)),
            max_changed_files=8,
            path_identity=_path_identity(),
        )
    )

    workspace_parent = tmp_path / "PF5 workspaces"
    workspace_parent.mkdir()
    startup = PackagedLocalProductFactoryStartup(
        workspace_parent=workspace_parent.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(python_executable,),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=100_000,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=Path(sys.executable).resolve(),
    )
    node = ExecutionNode(
        NodeIdentity(
            NODE_ID,
            _platform(),
            "x86_64",
            "instance-packaged-local-1",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": PROJECT_ID,
        },
    )
    host = build_packaged_local_durable_build_host(
        store,
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
        node=node,
        startup=startup,
        trusted_authority=execution_authority,
        output_policies=output_policies,
    )

    submitted = host.submit(spec)
    prepared = host.prepare(spec.request.work_id)

    dangerous_plan_argv = graph.components[0].build_commands[0]
    assert submitted.grant.argv == (python_executable, "-m", "build")
    assert submitted.grant.argv != dangerous_plan_argv
    assert prepared.grant.argv == submitted.grant.argv
    assert prepared.node_id == NODE_ID
    assert host.checkpoints.latest().snapshot.sequence == 2

def test_reviewed_candidate_executes_real_build_and_enters_durable_staging_once(
    tmp_path: Path,
) -> None:
    root, source_sha = _real_repository(tmp_path)
    graph = _graph()
    coordinator = _accepted_coordinator(graph, result_sha=source_sha)
    graph_authority = RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:real-build",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=graph,
        dependency_edges=(),
    )
    spec = reviewed_component_build_spec(
        authority=graph_authority,
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        policies=BuildAdmissionPolicies(_platform()),
    )
    python_executable = str(Path(sys.executable).resolve())
    execution_authority = ExecutionAuthority(
        ProjectExecutionAuthority(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            permissions=frozenset({"build_release"}),
            allowed_node_ids=(NODE_ID,),
            allowed_workspace_paths=("products/build",),
            network_scopes=(),
            credential_refs=(),
            commands=(
                ApprovedBuildCommand(
                    "build",
                    (
                        python_executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "Path('artifact.bin').write_bytes(b'pf4-pf6-real-build')"
                        ),
                    ),
                ),
            ),
            evidence_refs=("authority://packaged-build/integration-real",),
        )
    )
    output_policies = OutputPolicies(
        BuildOutputPolicy(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            allowed_paths=AllowedPathPolicy(("products/build",)),
            max_changed_files=8,
            path_identity=_path_identity(),
        )
    )
    workspace_parent = tmp_path / "PF5 real integration workspaces"
    workspace_parent.mkdir()
    startup = PackagedLocalProductFactoryStartup(
        workspace_parent=workspace_parent.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(python_executable,),
            resource_budget=ResourceBudget(
                timeout_seconds=60,
                max_output_bytes=1024 * 1024,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=_git(),
    )
    node = ExecutionNode(
        NodeIdentity(
            NODE_ID,
            _platform(),
            "x86_64",
            "instance-packaged-local-real",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )
    store = SQLiteStore(tmp_path / "real-build-loop.db")
    store.initialize()
    _bind_repository(store, graph, root)
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": PROJECT_ID,
        },
    )
    build_host = build_packaged_local_durable_build_host(
        store,
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
        node=node,
        startup=startup,
        trusted_authority=execution_authority,
        output_policies=output_policies,
    )

    build_host.submit(spec)
    prepared = build_host.prepare(spec.request.work_id)
    dispatch = build_host.begin_dispatch(spec.request.work_id)
    completed = build_host.execute(spec.request.work_id)

    assert prepared.grant.argv != graph.components[0].build_commands[0]
    assert dispatch.source_sha == source_sha
    assert completed.state.value == "succeeded"
    assert completed.evidence is not None
    assert completed.evidence.release_sha == source_sha
    assert (root / "products" / "build" / "artifact.bin").exists() is False
    status = subprocess.run(
        (str(_git()), "-C", str(root), "status", "--porcelain"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    assert status == ""

    provider = HealthyStagingProvider()
    deployment = DurableDeploymentFabric(
        provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(store),
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
    )
    handoff_authority = HandoffAuthority(
        BuildDeploymentAuthority(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            release_version="1.0.0-integration",
            staging_environment=EnvironmentIdentity(
                "staging-packaged-integration",
                PROJECT_ID,
                EnvironmentTier.STAGING,
                "integration-staging-provider",
            ),
            migration_refs=(),
        )
    )
    handoff = BuildDeploymentHandoff(
        build_host,
        deployment,
        handoff_authority,
    )

    deployed = handoff.deploy_staging(spec.request.work_id)
    duplicate = handoff.deploy_staging(spec.request.work_id)

    assert deployed.state is DeploymentState.HEALTHY
    assert deployed.intent.release.source_sha == source_sha
    assert deployed.intent.release.artifact_digest == completed.evidence.artifact_digest
    assert duplicate == deployed
    assert provider.deploy_calls == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_build = build_packaged_local_durable_build_host(
        restarted_store,
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
        node=node,
        startup=startup,
        trusted_authority=execution_authority,
        output_policies=output_policies,
    )
    restarted_provider = HealthyStagingProvider()
    restarted_deployment = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
    )
    repeated_after_restart = BuildDeploymentHandoff(
        restarted_build,
        restarted_deployment,
        handoff_authority,
    ).deploy_staging(spec.request.work_id)

    assert repeated_after_restart == deployed
    assert restarted_provider.deploy_calls == 0

