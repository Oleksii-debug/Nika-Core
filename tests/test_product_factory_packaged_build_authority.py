from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import nika_core.product_factory_packaged_build_authority as authority_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_execution import (
    BuildExecutionPortError,
    BuildExecutionState,
)
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


@pytest.mark.parametrize("separator", ("\u0085", "\u2028", "\u2029"))
def test_template_rejects_unicode_line_separators_in_authority_sets(
    separator: str,
) -> None:
    base = _template()

    with pytest.raises(PackagedBuildAuthorityError, match="canonical text"):
        replace(
            base,
            required_features=frozenset({"build", f"gpu{separator}scope"}),
        )

    with pytest.raises(PackagedBuildAuthorityError, match="canonical text"):
        replace(
            base,
            required_toolchains=frozenset({"python", f"tool{separator}chain"}),
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


def test_repeat_bind_rejects_resource_scope_drift_for_same_work(
    tmp_path: Path,
) -> None:
    _store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    spec = _admit(runtime)
    bound, snapshot = runtime.authorities.bound_snapshot(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
    )
    drifted = replace(
        spec,
        request=replace(
            spec.request,
            resources=ResourceEnvelope(2, 2048, 4096),
        ),
    )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="does not match the packaged build authority snapshot",
    ):
        runtime.authorities.bind(
            spec=drifted,
            component_id=bound.component_id,
            candidate_work_id=bound.candidate_work_id,
            review_fingerprint=bound.review_fingerprint,
            spec_version=bound.spec_version,
            row_version=bound.row_version,
            graph_digest=bound.graph_digest,
            authority=snapshot,
        )


def test_template_drift_invalidates_execution_but_preserves_bound_output_inspection(
    tmp_path: Path,
) -> None:
    _store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    spec = _admit(runtime)

    runtime.authorities.configure(
        _template(argv_suffix=("--wheel",), max_changed_files=3),
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

    output = runtime.output_policies.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
    )
    assert output.allowed_paths.roots == ("products/build",)
    assert output.max_changed_files == 8

    current = runtime.authorities.snapshot(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        component_id=COMPONENT_ID,
    )
    assert current.revision == 2
    assert current.template.max_changed_files == 3

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="different packaged authority",
    ):
        _admit(runtime)


def test_reconfigured_restart_restores_uncertain_effect_without_stale_replay(
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
        recovery_authority=runtime.recovery_execution,
    )
    host.submit(spec)
    host.prepare(spec.request.work_id)
    host.begin_dispatch(spec.request.work_id)

    class LostAcknowledgementPort:
        def run(self, _dispatch):
            raise BuildExecutionPortError("simulated acknowledgement loss")

        def inspect(self, _dispatch):
            return None

    host.node_port = LostAcknowledgementPort()
    uncertain = host.execute(spec.request.work_id)
    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED

    runtime.authorities.configure(
        _template(argv_suffix=("--wheel",), max_changed_files=3),
        expected_revision=1,
    )
    restarted_runtime = PackagedBuildAuthorityRuntime(
        PackagedBuildAuthorityStore(
            store,
            node=node,
            startup=startup,
        )
    )
    restarted = build_packaged_local_durable_build_host(
        store,
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
        node=node,
        startup=startup,
        trusted_authority=restarted_runtime.trusted_execution,
        output_policies=restarted_runtime.output_policies,
        recovery_authority=restarted_runtime.recovery_execution,
    )

    restored = restarted.snapshot().coordinator.records[0]
    assert restored.state is BuildExecutionState.RECONCILE_REQUIRED
    assert restarted.coordinator.trusted_authority is restarted_runtime.trusted_execution

    inspected = restarted.reconcile(spec.request.work_id)
    assert inspected.state is BuildExecutionState.RECONCILE_REQUIRED
    with pytest.raises(
        PackagedBuildAuthorityError,
        match="changed after PF5 work admission",
    ):
        restarted_runtime.trusted_execution.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
        )


def test_historical_bound_output_policy_fails_closed_on_history_tamper(
    tmp_path: Path,
) -> None:
    store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    spec = _admit(runtime)
    runtime.authorities.configure(
        _template(argv_suffix=("--wheel",)),
        expected_revision=1,
    )

    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE product_factory_build_authority_template_history "
            "SET template_json = ? "
            "WHERE project_id = ? AND repository_id = ? AND component_id = ? "
            "AND revision = ?",
            ("{}", PROJECT_ID, REPOSITORY_ID, COMPONENT_ID, 1),
        )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="does not match binding",
    ):
        runtime.output_policies.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
        )


def test_missing_bound_template_history_fails_closed_after_drift(
    tmp_path: Path,
) -> None:
    store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    spec = _admit(runtime)
    runtime.authorities.configure(
        _template(argv_suffix=("--wheel",)),
        expected_revision=1,
    )

    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM product_factory_build_authority_template_history "
            "WHERE project_id = ? AND repository_id = ? AND component_id = ? "
            "AND revision = ?",
            (PROJECT_ID, REPOSITORY_ID, COMPONENT_ID, 1),
        )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="historical packaged build authority is unavailable",
    ):
        runtime.output_policies.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
        )


def test_schema_v1_migration_backfills_current_template_history(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    startup = _startup(tmp_path)
    node = _node()
    template = _template()
    payload = authority_module._encode_template(template)
    digest = authority_module._digest_payload(payload)
    configured_at = "2026-10-06T00:00:00+00:00"

    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE product_factory_build_authority_schema ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE product_factory_build_authority_templates ("
            "project_id TEXT NOT NULL, repository_id TEXT NOT NULL, "
            "component_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0), "
            "template_json TEXT NOT NULL, template_digest TEXT NOT NULL, "
            "configured_at TEXT NOT NULL, "
            "PRIMARY KEY(project_id, repository_id, component_id))"
        )
        conn.execute(
            "CREATE TABLE product_factory_build_authority_bindings ("
            "work_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
            "repository_id TEXT NOT NULL, component_id TEXT NOT NULL, "
            "candidate_work_id TEXT NOT NULL, source_sha TEXT NOT NULL, "
            "review_fingerprint TEXT NOT NULL, spec_version INTEGER NOT NULL, "
            "row_version INTEGER NOT NULL, graph_digest TEXT NOT NULL, "
            "template_revision INTEGER NOT NULL, template_digest TEXT NOT NULL, "
            "bound_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO product_factory_build_authority_schema VALUES (?, ?)",
            (1, configured_at),
        )
        conn.execute(
            "INSERT INTO product_factory_build_authority_templates "
            "(project_id, repository_id, component_id, revision, template_json, "
            "template_digest, configured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                PROJECT_ID,
                REPOSITORY_ID,
                COMPONENT_ID,
                1,
                payload,
                digest,
                configured_at,
            ),
        )

    authorities = PackagedBuildAuthorityStore(
        store,
        node=node,
        startup=startup,
    )
    snapshot = authorities.snapshot(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        component_id=COMPONENT_ID,
    )
    assert snapshot == authority_module.PackagedBuildAuthoritySnapshot(
        template,
        1,
        digest,
    )

    with store.connection() as conn:
        history = conn.execute(
            "SELECT revision, template_json, template_digest, configured_at "
            "FROM product_factory_build_authority_template_history "
            "WHERE project_id = ? AND repository_id = ? AND component_id = ?",
            (PROJECT_ID, REPOSITORY_ID, COMPONENT_ID),
        ).fetchall()
        versions = conn.execute(
            "SELECT version FROM product_factory_build_authority_schema "
            "ORDER BY version"
        ).fetchall()

    assert [row["revision"] for row in history] == [1]
    assert history[0]["template_json"] == payload
    assert history[0]["template_digest"] == digest
    assert history[0]["configured_at"] == configured_at
    assert [row["version"] for row in versions] == [1, 2]


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


@pytest.mark.parametrize(
    "credential_option",
    (
        "--token",
        "--password",
        "--api-key",
        "--access-token",
        "--refresh-token",
    ),
)
def test_split_form_credential_material_is_rejected_before_storage(
    credential_option: str,
) -> None:
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
            argv=(executable, credential_option, "plaintext-secret"),
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


def test_forged_template_cannot_bypass_constructor_before_durable_storage(
    tmp_path: Path,
) -> None:
    _store, _startup_value, _node_value, runtime = _runtime(tmp_path)
    valid = _template()
    forged = object.__new__(PackagedBuildAuthorityTemplate)
    for name in (
        "project_id",
        "repository_id",
        "component_id",
        "node_id",
        "platform",
        "workspace_relpath",
        "required_features",
        "required_toolchains",
        "resources",
        "command_id",
        "output_paths",
        "max_changed_files",
        "lease_seconds",
        "require_gpu",
    ):
        object.__setattr__(forged, name, getattr(valid, name))
    object.__setattr__(
        forged,
        "argv",
        (str(Path(sys.executable).resolve()), "--token=constructor-bypass"),
    )

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="credential material",
    ):
        runtime.authorities.configure(
            forged,
            expected_revision=1,
        )

    snapshot = runtime.authorities.snapshot(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        component_id=COMPONENT_ID,
    )
    assert snapshot.revision == 1
    assert "constructor-bypass" not in " ".join(snapshot.template.argv)


def test_template_rejects_non_exact_resource_scalars() -> None:
    executable = str(Path(sys.executable).resolve())

    with pytest.raises(
        PackagedBuildAuthorityError,
        match="cpu_cores",
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
            resources=ResourceEnvelope(True, 1024, 2048),
            command_id="build",
            argv=(executable, "-m", "build"),
            output_paths=("products/build",),
            max_changed_files=8,
            lease_seconds=120,
        )
