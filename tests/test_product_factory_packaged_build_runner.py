from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.product_factory_packaged_build_runner as runner_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_execution import BuildExecutionState
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_multi_repository import (
    MultiRepositoryExecutionState,
    RepositoryGraphAuthority,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityStore,
    PackagedBuildAuthorityTemplate,
)
from nika_core.product_factory_packaged_build_runner import (
    PackagedReviewedBuildRunner,
    PackagedReviewedBuildRunnerError,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory
from nika_core.toolsmith.contracts import CodingResult, ResourceBudget, TestEvidence

PROJECT_ID = "product-packaged-reviewed-runner"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop"
NODE_ID = "packaged-build-node"
HOST_TASK_ID = "host-task-packaged-reviewed-runner"
BASE_SHA = "a" * 40
RESULT_SHA = "b" * 40
DIFF_DIGEST = "c" * 64
GRAPH_DIGEST = "d" * 64
PRODUCER = "worker:builder"
REVIEWER = "worker:reviewer"
TEST_COMMAND = ("python", "-m", "pytest", "tests")


class ReviewAuthority:
    def verify(self, subject, evidence_refs) -> bool:
        return (
            subject.project_id == PROJECT_ID
            and subject.component_id == COMPONENT_ID
            and subject.producer_actor_id == PRODUCER
            and subject.reviewer_id == REVIEWER
            and subject.accepted is True
            and evidence_refs == ("review://accepted",)
        )


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


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
                build_commands=(("candidate-controlled-shell", "unsafe"),),
                test_commands=(TEST_COMMAND,),
                release_identity="desktop-release",
            ),
        ),
    )


def _coordinator(*, accepted: bool) -> ProductFactoryCoordinator:
    graph = _graph()
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
    if not accepted:
        return coordinator
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


def _authority() -> RepositoryGraphAuthority:
    return RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:runner",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=_graph(),
        dependency_edges=(),
    )


def _startup(tmp_path: Path) -> PackagedLocalProductFactoryStartup:
    executable = str(Path(sys.executable).resolve())
    workspace = tmp_path / "PF5 runner workspaces"
    workspace.mkdir()
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace.resolve(),
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


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(
            NODE_ID,
            _platform(),
            "x86_64",
            "instance-reviewed-runner",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )


def _template() -> PackagedBuildAuthorityTemplate:
    executable = str(Path(sys.executable).resolve())
    return PackagedBuildAuthorityTemplate(
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
        argv=(executable, "-c", "print('trusted-build')"),
        output_paths=("products/build",),
        max_changed_files=8,
        lease_seconds=120,
    )


def _prepared(
    store: SQLiteStore,
    *,
    accepted: bool,
    task_project_id: str = PROJECT_ID,
) -> PreparedProductFactory:
    TaskQueue(store).create_exact(
        task_id=HOST_TASK_ID,
        workspace_id="test.product-factory",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": task_project_id,
        },
    )
    state = MultiRepositoryExecutionState(
        authority=_authority(),
        binding=None,  # runner consumes only durable authority/coordinator identity
        coordinator=_coordinator(accepted=accepted),
    )
    return PreparedProductFactory(HOST_TASK_ID, state)


def _runner(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    startup = _startup(tmp_path)
    node = _node()
    authorities = PackagedBuildAuthorityStore(
        store,
        node=node,
        startup=startup,
    )
    authorities.configure(_template(), expected_revision=0)
    return store, PackagedReviewedBuildRunner(store, node, startup)


class SuccessfulHost:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.spec = None

    def submit(self, spec):
        self.calls.append("submit")
        self.spec = spec
        return SimpleNamespace(state=BuildExecutionState.PENDING, evidence=None)

    def prepare(self, work_id: str):
        self.calls.append(f"prepare:{work_id}")
        return SimpleNamespace(state=BuildExecutionState.PREPARED, evidence=None)

    def begin_dispatch(self, work_id: str):
        self.calls.append(f"begin:{work_id}")
        return SimpleNamespace(work_id=work_id)

    def execute(self, work_id: str):
        self.calls.append(f"execute:{work_id}")
        return SimpleNamespace(
            state=BuildExecutionState.SUCCEEDED,
            evidence=SimpleNamespace(artifact_digest="f" * 64),
        )

    def reconcile(self, work_id: str):
        raise AssertionError(f"unexpected reconcile for {work_id}")


class ReconcileHost:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit(self, spec):
        self.calls.append("submit")
        return SimpleNamespace(
            state=BuildExecutionState.RECONCILE_REQUIRED,
            evidence=None,
        )

    def reconcile(self, work_id: str):
        self.calls.append(f"reconcile:{work_id}")
        return SimpleNamespace(
            state=BuildExecutionState.SUCCEEDED,
            evidence=SimpleNamespace(artifact_digest="1" * 64),
        )

    def prepare(self, work_id: str):
        raise AssertionError(f"reconcile path must not prepare {work_id}")

    def begin_dispatch(self, work_id: str):
        raise AssertionError(f"reconcile path must not dispatch {work_id}")

    def execute(self, work_id: str):
        raise AssertionError(f"reconcile path must not replay {work_id}")


def test_runner_admits_accepted_candidate_and_drives_incumbent_pf5_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, runner = _runner(tmp_path)
    prepared = _prepared(store, accepted=True)
    host = SuccessfulHost()
    monkeypatch.setattr(
        runner_module,
        "build_packaged_local_durable_build_host",
        lambda *args, **kwargs: host,
    )

    outcomes = runner.advance(prepared)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.component_id == COMPONENT_ID
    assert outcome.candidate_work_id != outcome.build_work_id
    assert outcome.state is BuildExecutionState.SUCCEEDED
    assert outcome.artifact_digest == "f" * 64
    assert host.spec.grant if hasattr(host.spec, "grant") else True
    assert host.spec.scope.command_id == "build"
    assert host.spec.scope.network_scopes == ()
    assert host.spec.scope.credential_refs == ()
    assert host.calls == [
        "submit",
        f"prepare:{outcome.build_work_id}",
        f"begin:{outcome.build_work_id}",
        f"execute:{outcome.build_work_id}",
    ]


def test_runner_reconciles_restored_uncertain_work_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, runner = _runner(tmp_path)
    prepared = _prepared(store, accepted=True)
    host = ReconcileHost()
    monkeypatch.setattr(
        runner_module,
        "build_packaged_local_durable_build_host",
        lambda *args, **kwargs: host,
    )

    outcomes = runner.advance(prepared)

    assert outcomes[0].state is BuildExecutionState.SUCCEEDED
    assert host.calls == [
        "submit",
        f"reconcile:{outcomes[0].build_work_id}",
    ]


def test_runner_does_not_create_pf5_work_before_independent_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, runner = _runner(tmp_path)
    prepared = _prepared(store, accepted=False)

    def forbidden_builder(*args, **kwargs):
        raise AssertionError("PF5 host must not be built without ACCEPTED PF4 work")

    monkeypatch.setattr(
        runner_module,
        "build_packaged_local_durable_build_host",
        forbidden_builder,
    )

    assert runner.advance(prepared) == ()


def test_runner_rejects_prepared_state_from_different_host_task_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, runner = _runner(tmp_path)
    prepared = _prepared(
        store,
        accepted=True,
        task_project_id="product-other",
    )

    def forbidden_builder(*args, **kwargs):
        raise AssertionError("mismatched host task must fail before PF5 composition")

    monkeypatch.setattr(
        runner_module,
        "build_packaged_local_durable_build_host",
        forbidden_builder,
    )

    with pytest.raises(
        PackagedReviewedBuildRunnerError,
        match="host task identity",
    ):
        runner.advance(prepared)
