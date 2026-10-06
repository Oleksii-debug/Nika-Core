from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_deployment_handoff import (
    BuildDeploymentAuthority,
    BuildDeploymentHandoff,
)
from nika_core.product_factory_build_execution import (
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionResult,
    BuildExecutionState,
)
from nika_core.product_factory_build_execution_host import DurableBuildExecutionHost
from nika_core.product_factory_build_execution_persistence import (
    SQLiteBuildExecutionCheckpointStore,
)
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
    ExecutionNodeRegistry,
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
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
    PackagedBuildAuthorityStore,
    PackagedBuildAuthorityTemplate,
)
from nika_core.product_factory_packaged_build_loop import (
    PackagedBuildLoopError,
    PackagedProductFactoryBuildLoop,
    build_packaged_product_factory_build_loop,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import ChangedFile, CodingResult, ResourceBudget, TestEvidence

PROJECT_ID = "product-packaged-build-loop-runtime"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop"
BASE_SHA = "a" * 40
RESULT_SHA = "b" * 40
DIFF_DIGEST = "c" * 64
GRAPH_DIGEST = "d" * 64
NODE_ID = "packaged-build-node"
ARTIFACT_DIGEST = "f" * 64
PRODUCER = "worker:builder"
REVIEWER = "worker:independent-reviewer"
TEST_COMMAND = ("python", "-m", "pytest", "tests")
NOW = datetime(2026, 10, 6, 20, 5, tzinfo=UTC)


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


class _ReviewAuthority:
    def verify(self, subject, evidence_refs) -> bool:
        return (
            subject.project_id == PROJECT_ID
            and subject.component_id == COMPONENT_ID
            and subject.producer_actor_id == PRODUCER
            and subject.reviewer_id == REVIEWER
            and subject.accepted is True
            and evidence_refs == ("review://accepted",)
        )


class _Availability:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self, node_id: str) -> bool:
        return self.available and node_id == NODE_ID


class _BuildNode:
    def __init__(self) -> None:
        self.run_calls = 0
        self.inspect_calls = 0
        self.receipts: dict[str, BuildExecutionResult] = {}

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        result = BuildExecutionResult(
            source_sha=dispatch.source_sha,
            artifact_digest=ARTIFACT_DIGEST,
            succeeded=True,
            uncertain=False,
            evidence_refs=("build://packaged/success",),
            completed_at=NOW,
        )
        self.receipts[dispatch.dispatch_id] = result
        return result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        self.inspect_calls += 1
        return self.receipts.get(dispatch.dispatch_id)

    def collect(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> tuple[ChangedFile, ...]:
        assert result.source_sha == dispatch.source_sha
        return (
            ChangedFile(
                "products/build/artifact.bin",
                ARTIFACT_DIGEST,
                32,
            ),
        )


class _HealthyStagingProvider:
    def __init__(self) -> None:
        self.deploy_calls = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        return ProviderDeploymentResult(True, False, ("deploy://ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://ok",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError(
            f"healthy staging release must not roll back: {previous_release_sha}"
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect://healthy",),
            release=intent.release,
        )


@dataclass
class _DeploymentAuthority:
    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        if project_id != PROJECT_ID or repository_id != REPOSITORY_ID:
            raise AssertionError("unexpected packaged staging identity")
        return BuildDeploymentAuthority(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
            release_version="1.0.0-packaged-proof",
            staging_environment=EnvironmentIdentity(
                "staging-packaged-build-loop",
                PROJECT_ID,
                EnvironmentTier.STAGING,
                "test-provider",
            ),
            migration_refs=(),
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


def _graph_authority(graph: ProductRepositoryGraph) -> RepositoryGraphAuthority:
    return RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:packaged-runtime",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=graph,
        dependency_edges=(),
    )


def _accepted_coordinator(graph: ProductRepositoryGraph) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(
        graph,
        review_authority=_ReviewAuthority(),
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
            result_sha=RESULT_SHA,
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
            evidence_refs=("review://accepted",),
        ),
    )
    return coordinator


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(
            NODE_ID,
            _platform(),
            "x86_64",
            "instance-packaged-build-runtime",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )


def _startup(tmp_path: Path) -> PackagedLocalProductFactoryStartup:
    executable = str(Path(sys.executable).resolve())
    workspace_parent = tmp_path / "PF5 packaged runtime workspaces"
    workspace_parent.mkdir(exist_ok=True)
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace_parent.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(executable,),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=100_000,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=Path(sys.executable).resolve(),
    )


def _runtime(
    store: SQLiteStore,
    startup: PackagedLocalProductFactoryStartup,
    node: ExecutionNode,
    *,
    configure: bool,
) -> PackagedBuildAuthorityRuntime:
    authorities = PackagedBuildAuthorityStore(
        store,
        node=node,
        startup=startup,
    )
    if configure:
        executable = str(Path(sys.executable).resolve())
        authorities.configure(
            PackagedBuildAuthorityTemplate(
                project_id=PROJECT_ID,
                repository_id=REPOSITORY_ID,
                component_id=COMPONENT_ID,
                node_id=NODE_ID,
                platform=_platform(),
                workspace_relpath="products/build",
                required_features=frozenset({"build"}),
                required_toolchains=frozenset({"python"}),
                resources=ResourceEnvelope(1, 1024, 2048),
                command_id="build",
                argv=(executable, "-c", "print('packaged build')"),
                output_paths=("products/build",),
                max_changed_files=8,
                lease_seconds=120,
            ),
            expected_revision=0,
        )
    return PackagedBuildAuthorityRuntime(authorities)


def _host_task(store: SQLiteStore) -> str:
    return TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": PROJECT_ID,
        },
    ).task_id


def _build_host(
    store: SQLiteStore,
    *,
    host_task_id: str,
    runtime: PackagedBuildAuthorityRuntime,
    node: ExecutionNode,
    node_port: _BuildNode,
    available: bool = True,
) -> DurableBuildExecutionHost:
    registry = ExecutionNodeRegistry()
    registry.register(node)
    coordinator = BuildExecutionCoordinator(
        registry,
        _Availability(available),
        runtime.trusted_execution,
    )
    checkpoints = SQLiteBuildExecutionCheckpointStore(
        store,
        host_task_id,
        PROJECT_ID,
    )
    host = DurableBuildExecutionHost(
        coordinator,
        node_port,
        node_port,
        runtime.output_policies,
        checkpoints,
    )
    if checkpoints.has_checkpoint():
        host.restore_latest(now=NOW)
    return host


def _deployment(
    store: SQLiteStore,
    *,
    host_task_id: str,
    provider: _HealthyStagingProvider,
    restore: bool = False,
) -> DurableDeploymentFabric:
    checkpoint_host = ProductFactoryDeploymentCheckpointHost(store)
    if restore:
        return DurableDeploymentFabric.restore_latest(
            provider,
            checkpoint_host=checkpoint_host,
            host_task_id=host_task_id,
            project_id=PROJECT_ID,
        )
    return DurableDeploymentFabric(
        provider,
        checkpoint_host=checkpoint_host,
        host_task_id=host_task_id,
        project_id=PROJECT_ID,
    )


def _loop(
    runtime: PackagedBuildAuthorityRuntime,
    build_host: DurableBuildExecutionHost,
    deployment: DurableDeploymentFabric,
) -> PackagedProductFactoryBuildLoop:
    return PackagedProductFactoryBuildLoop(
        runtime,
        build_host,
        BuildDeploymentHandoff(
            build_host,
            deployment,
            _DeploymentAuthority(),
        ),
    )


def test_production_authority_build_runs_and_stages_once_across_restart(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "packaged-build-loop.db")
    store.initialize()
    node = _node()
    startup = _startup(tmp_path)
    runtime = _runtime(store, startup, node, configure=True)
    host_task_id = _host_task(store)
    node_port = _BuildNode()
    provider = _HealthyStagingProvider()
    loop = _loop(
        runtime,
        _build_host(
            store,
            host_task_id=host_task_id,
            runtime=runtime,
            node=node,
            node_port=node_port,
        ),
        _deployment(store, host_task_id=host_task_id, provider=provider),
    )
    graph = _graph()
    coordinator = _accepted_coordinator(graph)

    first = loop.advance_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=coordinator,
        component_id=COMPONENT_ID,
    )
    repeated = loop.advance_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=coordinator,
        component_id=COMPONENT_ID,
    )

    assert first.build.state is BuildExecutionState.SUCCEEDED
    assert first.deployment is not None
    assert first.deployment.state is DeploymentState.HEALTHY
    assert repeated == first
    assert node_port.run_calls == 1
    assert node_port.inspect_calls == 0
    assert provider.deploy_calls == 1
    assert first.build.grant.argv != graph.components[0].build_commands[0]

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_runtime = _runtime(
        restarted_store,
        startup,
        node,
        configure=False,
    )
    restarted_node_port = _BuildNode()
    restarted_provider = _HealthyStagingProvider()
    restarted_loop = _loop(
        restarted_runtime,
        _build_host(
            restarted_store,
            host_task_id=host_task_id,
            runtime=restarted_runtime,
            node=node,
            node_port=restarted_node_port,
        ),
        _deployment(
            restarted_store,
            host_task_id=host_task_id,
            provider=restarted_provider,
            restore=True,
        ),
    )

    after_restart = restarted_loop.advance_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=coordinator,
        component_id=COMPONENT_ID,
    )

    assert after_restart.build.state is BuildExecutionState.SUCCEEDED
    assert after_restart.deployment == first.deployment
    assert restarted_node_port.run_calls == 0
    assert restarted_node_port.inspect_calls == 0
    assert restarted_provider.deploy_calls == 0


def test_crash_left_dispatch_is_inspected_once_and_never_replayed(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "crash-left-build-loop.db")
    store.initialize()
    node = _node()
    startup = _startup(tmp_path)
    runtime = _runtime(store, startup, node, configure=True)
    host_task_id = _host_task(store)
    original_node_port = _BuildNode()
    host = _build_host(
        store,
        host_task_id=host_task_id,
        runtime=runtime,
        node=node,
        node_port=original_node_port,
    )
    graph = _graph()
    coordinator = _accepted_coordinator(graph)
    spec = runtime.admit_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=coordinator,
        component_id=COMPONENT_ID,
    )
    host.submit(spec)
    host.prepare(spec.request.work_id, now=NOW)
    host.begin_dispatch(spec.request.work_id, now=NOW)

    restarted_runtime = _runtime(store, startup, node, configure=False)
    restarted_node_port = _BuildNode()
    restarted_host = _build_host(
        store,
        host_task_id=host_task_id,
        runtime=restarted_runtime,
        node=node,
        node_port=restarted_node_port,
    )
    provider = _HealthyStagingProvider()
    loop = _loop(
        restarted_runtime,
        restarted_host,
        _deployment(store, host_task_id=host_task_id, provider=provider),
    )

    result = loop.advance_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=coordinator,
        component_id=COMPONENT_ID,
    )

    assert result.build.state is BuildExecutionState.RECONCILE_REQUIRED
    assert result.deployment is None
    assert original_node_port.run_calls == 0
    assert restarted_node_port.run_calls == 0
    assert restarted_node_port.inspect_calls == 1
    assert provider.deploy_calls == 0


def test_waiting_for_node_never_enters_staging(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "waiting-build-loop.db")
    store.initialize()
    node = _node()
    startup = _startup(tmp_path)
    runtime = _runtime(store, startup, node, configure=True)
    host_task_id = _host_task(store)
    node_port = _BuildNode()
    provider = _HealthyStagingProvider()
    loop = _loop(
        runtime,
        _build_host(
            store,
            host_task_id=host_task_id,
            runtime=runtime,
            node=node,
            node_port=node_port,
            available=False,
        ),
        _deployment(store, host_task_id=host_task_id, provider=provider),
    )
    graph = _graph()

    result = loop.advance_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=_accepted_coordinator(graph),
        component_id=COMPONENT_ID,
    )

    assert result.build.state is BuildExecutionState.WAITING_FOR_NODE
    assert result.deployment is None
    assert node_port.run_calls == 0
    assert node_port.inspect_calls == 0
    assert provider.deploy_calls == 0


def test_split_pf5_authority_composition_is_rejected(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "split-authority.db")
    store.initialize()
    node = _node()
    startup = _startup(tmp_path)
    runtime = _runtime(store, startup, node, configure=True)
    host_task_id = _host_task(store)
    node_port = _BuildNode()
    registry = ExecutionNodeRegistry()
    registry.register(node)

    class _OtherAuthority:
        def resolve(self, **kwargs: Any):
            return runtime.trusted_execution.resolve(**kwargs)

    host = DurableBuildExecutionHost(
        BuildExecutionCoordinator(
            registry,
            _Availability(),
            _OtherAuthority(),
        ),
        node_port,
        node_port,
        runtime.output_policies,
        SQLiteBuildExecutionCheckpointStore(
            store,
            host_task_id,
            PROJECT_ID,
        ),
    )
    deployment = _deployment(
        store,
        host_task_id=host_task_id,
        provider=_HealthyStagingProvider(),
    )

    with pytest.raises(PackagedBuildLoopError, match="exact packaged build authority"):
        _loop(runtime, host, deployment)


def test_production_builder_wires_exact_packaged_authorities(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "builder.db")
    store.initialize()
    node = _node()
    startup = _startup(tmp_path)
    runtime = _runtime(store, startup, node, configure=True)
    host_task_id = _host_task(store)
    deployment = _deployment(
        store,
        host_task_id=host_task_id,
        provider=_HealthyStagingProvider(),
    )

    loop = build_packaged_product_factory_build_loop(
        store,
        host_task_id=host_task_id,
        project_id=PROJECT_ID,
        node=node,
        startup=startup,
        authority_runtime=runtime,
        deployment=deployment,
        deployment_authority=_DeploymentAuthority(),
    )

    assert loop.build_host.coordinator.trusted_authority is runtime.trusted_execution
    assert loop.build_host.output_policies is runtime.output_policies
    assert loop.handoff.build_host is loop.build_host
    assert loop.handoff.deployment is deployment
