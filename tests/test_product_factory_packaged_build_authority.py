from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

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
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityError,
    PackagedBuildAuthorityRuntime,
    PackagedBuildAuthorityStore,
    PackagedBuildAuthorityTemplate,
)
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import CodingResult, ResourceBudget, TestEvidence

PROJECT_ID = "product-packaged-build-authority"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop"
NODE_ID = "packaged-build-node"
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


def _graph_authority(
    graph: ProductRepositoryGraph,
) -> RepositoryGraphAuthority:
    return RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:1",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=graph,
        dependency_edges=(),
    )


def _startup(tmp_path: Path) -> PackagedLocalProductFactoryStartup:
    executable = str(Path(sys.executable).resolve())
    workspace_parent = tmp_path / "PF5 workspaces"
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


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(
            NODE_ID,
            _platform(),
            "x86_64",
            "instance-packaged-build-node",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )


def _template(
    *,
    argv_suffix: tuple[str, ...] = (),
    node_id: str = NODE_ID,
    max_changed_files: int = 8,
) -> PackagedBuildAuthorityTemplate:
    executable = str(Path(sys.executable).resolve())
    return PackagedBuildAuthorityTemplate(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        component_id=COMPONENT_ID,
        node_id=node_id,
        platform=_platform(),
        workspace_relpath="products/build",
        required_features=frozenset({"build"}),
        required_toolchains=frozenset({"python"}),
        resources=ResourceEnvelope(1, 1024, 2048),
        command_id="build",
        argv=(executable, "-m", "build", *argv_suffix),
        output_paths=("products/build",),
        max_changed_files=max_changed_files,
        lease_seconds=120,
    )


def _runtime(
    tmp_path: Path,
) -> tuple[
    SQLiteStore,
    PackagedLocalProductFactoryStartup,
    ExecutionNode,
    PackagedBuildAuthorityRuntime,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    startup = _startup(tmp_path)
    node = _node()
    authorities = PackagedBuildAuthorityStore(
        store,
        node=node,
        startup=startup,
    )
    authorities.configure(
        _template(),
        expected_revision=0,
    )
    return (
        store,
        startup,
        node,
        PackagedBuildAuthorityRuntime(authorities),
    )


def _admit(
    runtime: PackagedBuildAuthorityRuntime,
):
    graph = _graph()
    return runtime.admit_reviewed_component(
        authority=_graph_authority(graph),
        coordinator=_accepted_coordinator(graph),
        component_id=COMPONENT_ID,
    )


def test_packaged_authority_drives_admission_execution_output_and_restart(
    tmp_path: Path,
) -> None:
    store, startup, node, runtime = _runtime(tmp_path)
    spec = _admit(runtime)

    execution = runtime.trusted_execution.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
    )
    output = runtime.output_policies.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
    )

    assert execution.commands[0].argv == (
        str(Path(sys.executable).resolve()),
        "-m",
        "build",
    )
    assert execution.commands[0].argv != (
        _graph().components[0].build_commands[0]
    )
    assert execution.allowed_node_ids == (NODE_ID,)
    assert execution.network_scopes == ()
    assert execution.credential_refs == ()
    assert output.allowed_paths.roots == ("products/build",)
    assert output.max_changed_files == 8

    restarted = PackagedBuildAuthorityRuntime(
        PackagedBuildAuthorityStore(
            store,
            node=node,
            startup=startup,
        )
    )
    assert restarted.trusted_execution.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
    ) == execution
    assert restarted.output_policies.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
    ) == output


def test_packaged_authority_reaches_durable_pf5_prepared_state(
    tmp_path: Path,
) -> None:
    store, startup, node, runtime = _runtime(tmp_path)
    spec = _admit(runtime)
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
        trusted_authority=runtime.trusted_execution,
        output_policies=runtime.output_policies,
    )

    submitted = host.submit(spec)
    prepared = host.prepare(spec.request.work_id)

    assert submitted.grant.argv == (
        str(Path(sys.executable).resolve()),
        "-m",
        "build",
    )
    assert prepared.state is BuildExecutionState.PREPARED
    assert prepared.node_id == NODE_ID
    assert host.checkpoints.latest().snapshot.sequence == 2


def test_template_drift_invalidates_already_bound_work(
    tmp_path: Path,
) -> None:
    _store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    spec = _admit(runtime)

    runtime.authorities.configure(
        _template(argv_suffix=("--wheel",)),
        expected_revision=1,
    )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="changed after PF5 work admission",
    ):
        runtime.trusted_execution.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
        )
    with pytest.raises(
        PackagedBuildAuthorityError,
        match="different packaged authority",
    ):
        _admit(runtime)


def test_stale_configure_revision_is_fail_closed(tmp_path: Path) -> None:
    _store, _startup_value, _node_value, runtime = _runtime(tmp_path)

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="reload before updating",
    ):
        runtime.authorities.configure(
            _template(argv_suffix=("--wheel",)),
            expected_revision=0,
        )

    snapshot = runtime.authorities.snapshot(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        component_id=COMPONENT_ID,
    )
    assert snapshot.revision == 1
    assert snapshot.template.argv[-2:] == ("-m", "build")


def test_zero_output_ceiling_is_rejected_before_authority_storage(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authorities = PackagedBuildAuthorityStore(
        store,
        node=_node(),
        startup=_startup(tmp_path),
    )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="max_changed_files must be 1..10000",
    ):
        authorities.configure(
            _template(max_changed_files=0),
            expected_revision=0,
        )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="no packaged build authority",
    ):
        authorities.snapshot(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            component_id=COMPONENT_ID,
        )


def test_runtime_node_mismatch_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    startup = _startup(tmp_path)
    authorities = PackagedBuildAuthorityStore(
        store,
        node=_node(),
        startup=startup,
    )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="execution node",
    ):
        authorities.configure(
            _template(node_id="other-node"),
            expected_revision=0,
        )


def test_inline_credential_material_is_not_accepted_into_durable_argv() -> None:
    executable = str(Path(sys.executable).resolve())

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="credential material",
    ):
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
            argv=(executable, "--token=do-not-store-this"),
            output_paths=("products/build",),
            max_changed_files=8,
            lease_seconds=120,
        )


def test_wrong_work_identity_cannot_resolve_bound_authority(
    tmp_path: Path,
) -> None:
    _store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    spec = _admit(runtime)

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="identity does not match",
    ):
        runtime.trusted_execution.resolve(
            project_id=PROJECT_ID,
            repository_id="repo-other",
            work_id=spec.request.work_id,
        )
