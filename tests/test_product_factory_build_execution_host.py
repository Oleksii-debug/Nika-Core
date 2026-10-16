from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionError,
    BuildExecutionPortError,
    BuildExecutionResult,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    BuildExecutionState,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
    BuildExecutionCheckpointIntegrityError,
    BuildExecutionDurabilityError,
    BuildOutputPolicy,
    DurableBuildExecutionHost,
    SQLiteBuildExecutionCheckpointStore,
)
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.toolsmith.contracts import AllowedPathPolicy, ChangedFile

NOW = datetime(2026, 8, 23, 21, 0, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "1" * 64
FILE_DIGEST = "2" * 64


@dataclass
class Available:
    unavailable: set[str]

    def is_available(self, node_id: str) -> bool:
        return node_id not in self.unavailable


@dataclass
class Authority:
    value: ProjectExecutionAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


@dataclass
class OutputPolicies:
    value: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


@dataclass
class FileEvidence:
    changed_files: tuple[ChangedFile, ...] = ()
    calls: int = 0

    def collect(self, dispatch: BuildExecutionDispatch, result: BuildExecutionResult):
        self.calls += 1
        return self.changed_files


@dataclass
class Port:
    run_result: BuildExecutionResult | None = None
    inspect_result: BuildExecutionResult | None = None
    fail_run: bool = False
    programming_error: bool = False
    run_calls: int = 0
    inspect_calls: int = 0
    checkpoint: SQLiteBuildExecutionCheckpointStore | None = None
    observed_state: BuildExecutionState | None = None

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        if self.checkpoint is not None:
            latest = self.checkpoint.latest()
            record = latest.snapshot.coordinator.records[0]
            self.observed_state = record.state
            assert record.dispatch == dispatch
        if self.fail_run:
            raise BuildExecutionPortError("lost acknowledgement")
        if self.programming_error:
            raise RuntimeError("adapter crash after dispatch")
        assert self.run_result is not None
        return self.run_result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        self.inspect_calls += 1
        return self.inspect_result


def _node(node_id: str, platform: Platform) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(node_id, platform, "x86_64", f"instance-{node_id}"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(8, 16384, 65536),
    )


def _spec(work_id: str, platform: Platform, node_id: str) -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project-1",
            work_id,
            platform,
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(2, 2048, 4096),
        ),
        SHA,
        BuildExecutionScopeRequest(
            "repo-main",
            "products/build",
            (node_id,),
            ("pypi.org:443",),
            ("credref:package-index",),
            "build",
        ),
        120,
    )


def _authority(work_id: str, node_id: str) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        work_id,
        frozenset({"build_release"}),
        (node_id,),
        ("products",),
        ("pypi.org:443",),
        ("credref:package-index",),
        (ApprovedBuildCommand("build", ("python", "-m", "build")),),
        ("authority://trusted-plan/1",),
    )


def _result(*, uncertain: bool = False) -> BuildExecutionResult:
    return BuildExecutionResult(
        SHA,
        DIGEST,
        not uncertain,
        uncertain,
        ("evidence://build",),
        NOW,
    )


def _setup(
    tmp_path,
    *,
    work_id: str = "work-1",
    platform: Platform = Platform.LINUX,
    node_id: str = "linux-1",
    port: Port | None = None,
    changed_files: tuple[ChangedFile, ...] = (),
    unavailable: set[str] | None = None,
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    registry = ExecutionNodeRegistry()
    registry.register(_node(node_id, platform))
    authority = Authority(_authority(work_id, node_id))
    coordinator = BuildExecutionCoordinator(
        registry,
        Available(unavailable or set()),
        authority,
    )
    checkpoint = SQLiteBuildExecutionCheckpointStore(
        store,
        task.task_id,
        "project-1",
    )
    path_identity = (
        RepositoryPathIdentity.CASE_INSENSITIVE
        if platform is Platform.WINDOWS
        else RepositoryPathIdentity.CASE_SENSITIVE
    )
    policy = OutputPolicies(
        BuildOutputPolicy(
            "project-1",
            "repo-main",
            work_id,
            AllowedPathPolicy(("products/build",)),
            4,
            path_identity,
        )
    )
    files = FileEvidence(changed_files)
    node_port = port or Port(run_result=_result())
    host = DurableBuildExecutionHost(
        coordinator,
        node_port,
        files,
        policy,
        checkpoint,
    )
    return host, checkpoint, node_port, files, authority, task.task_id, store


def _dispatch(host: DurableBuildExecutionHost, spec: BuildExecutionSpec) -> None:
    host.submit(spec, now=NOW)
    host.prepare(spec.request.work_id, now=NOW)
    host.begin_dispatch(spec.request.work_id, now=NOW)


def test_effect_in_flight_is_in_sqlite_before_real_node_port_run(tmp_path) -> None:
    port = Port(run_result=_result())
    host, checkpoint, port, _, _, _, _ = _setup(tmp_path, port=port)
    port.checkpoint = checkpoint
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    _dispatch(host, spec)

    completed = host.execute("work-1", now=NOW)

    assert port.observed_state is BuildExecutionState.EFFECT_IN_FLIGHT
    assert completed.state is BuildExecutionState.SUCCEEDED
    latest = checkpoint.latest().snapshot
    assert latest.sequence == 5
    assert latest.coordinator.records[0].state is BuildExecutionState.SUCCEEDED
    assert latest.file_evidence[0].changed_files == ()


def test_lost_acknowledgement_never_blindly_replays_and_uses_inspect(tmp_path) -> None:
    port = Port(fail_run=True, inspect_result=_result())
    host, checkpoint, _, _, _, _, _ = _setup(tmp_path, port=port)
    _dispatch(host, _spec("work-1", Platform.LINUX, "linux-1"))

    uncertain = host.execute("work-1", now=NOW)
    repeated = host.execute("work-1", now=NOW)
    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED
    assert repeated.state is BuildExecutionState.RECONCILE_REQUIRED
    assert port.run_calls == 1

    port.fail_run = False
    reconciled = host.reconcile("work-1", now=NOW)

    assert reconciled.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 1
    assert port.inspect_calls == 1
    assert (
        checkpoint.latest().snapshot.coordinator.records[0].state
        is BuildExecutionState.SUCCEEDED
    )


def test_restart_crossing_effect_boundary_restores_dispatch_and_forces_inspection(tmp_path) -> None:
    port = Port(programming_error=True, inspect_result=_result())
    host, checkpoint, _, _, _, task_id, store = _setup(tmp_path, port=port)
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    _dispatch(host, spec)
    with pytest.raises(RuntimeError, match="adapter crash"):
        host.execute("work-1", now=NOW)
    assert (
        checkpoint.latest().snapshot.coordinator.records[0].state
        is BuildExecutionState.EFFECT_IN_FLIGHT
    )

    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    coordinator = BuildExecutionCoordinator(
        registry,
        Available(set()),
        Authority(_authority("work-1", "linux-1")),
    )
    restarted_port = Port(inspect_result=_result())
    restarted = DurableBuildExecutionHost(
        coordinator,
        restarted_port,
        FileEvidence(),
        OutputPolicies(
            BuildOutputPolicy(
                "project-1",
                "repo-main",
                "work-1",
                AllowedPathPolicy(("products/build",)),
                4,
                RepositoryPathIdentity.CASE_SENSITIVE,
            )
        ),
        SQLiteBuildExecutionCheckpointStore(store, task_id, "project-1"),
    )
    restored = restarted.restore_latest(now=NOW)

    assert restored.coordinator.records[0].state is BuildExecutionState.RECONCILE_REQUIRED
    assert registry.snapshot().leases == ()
    assert restarted_port.run_calls == 0
    completed = restarted.reconcile("work-1", now=NOW)
    assert completed.state is BuildExecutionState.SUCCEEDED
    assert restarted_port.inspect_calls == 1


def test_restart_rechecks_current_authority_and_fails_closed_on_drift(tmp_path) -> None:
    host, _, _, _, _, task_id, store = _setup(tmp_path)
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    host.submit(spec, now=NOW)
    host.prepare("work-1", now=NOW)

    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    drifted = _authority("work-1", "linux-1")
    drifted = ProjectExecutionAuthority(
        drifted.project_id,
        drifted.repository_id,
        drifted.work_id,
        frozenset({"run_tests"}),
        drifted.allowed_node_ids,
        drifted.allowed_workspace_paths,
        drifted.network_scopes,
        drifted.credential_refs,
        drifted.commands,
        ("authority://revoked",),
    )
    restarted = DurableBuildExecutionHost(
        BuildExecutionCoordinator(registry, Available(set()), Authority(drifted)),
        Port(inspect_result=_result()),
        FileEvidence(),
        OutputPolicies(
            BuildOutputPolicy(
                "project-1",
                "repo-main",
                "work-1",
                AllowedPathPolicy(("products/build",)),
                4,
                RepositoryPathIdentity.CASE_SENSITIVE,
            )
        ),
        SQLiteBuildExecutionCheckpointStore(store, task_id, "project-1"),
    )

    with pytest.raises(BuildExecutionError, match="no longer authorized"):
        restarted.restore_latest(now=NOW)


def test_changed_file_outside_host_policy_becomes_reconcile_required(tmp_path) -> None:
    changed = (ChangedFile("other/evil.py", FILE_DIGEST, 10),)
    host, checkpoint, port, files, _, _, _ = _setup(tmp_path, changed_files=changed)
    _dispatch(host, _spec("work-1", Platform.LINUX, "linux-1"))

    record = host.execute("work-1", now=NOW)

    assert record.state is BuildExecutionState.RECONCILE_REQUIRED
    assert record.evidence is None
    assert port.run_calls == 1
    assert files.calls == 1
    assert checkpoint.latest().snapshot.file_evidence == ()


def test_windows_case_variant_output_identity_fails_closed(tmp_path) -> None:
    changed = (
        ChangedFile("products/build/App.exe", FILE_DIGEST, 10),
        ChangedFile("products/build/app.exe", "3" * 64, 11),
    )
    host, _, port, _, _, _, _ = _setup(
        tmp_path,
        platform=Platform.WINDOWS,
        node_id="win-1",
        changed_files=changed,
    )
    _dispatch(host, _spec("work-1", Platform.WINDOWS, "win-1"))

    record = host.execute("work-1", now=NOW)

    assert record.state is BuildExecutionState.RECONCILE_REQUIRED
    assert port.run_calls == 1


def test_linux_case_variant_output_identity_is_distinct_and_normalized(tmp_path) -> None:
    changed = (
        ChangedFile("products/build/App", FILE_DIGEST, 10),
        ChangedFile("products/build/app", "3" * 64, 11),
    )
    host, checkpoint, _, _, _, _, _ = _setup(tmp_path, changed_files=changed)
    _dispatch(host, _spec("work-1", Platform.LINUX, "linux-1"))

    record = host.execute("work-1", now=NOW)

    assert record.state is BuildExecutionState.SUCCEEDED
    persisted = checkpoint.latest().snapshot.file_evidence[0]
    assert [item.path for item in persisted.changed_files] == [
        "products/build/App",
        "products/build/app",
    ]


def test_platform_unavailable_is_durable_waiting_without_node_effect(tmp_path) -> None:
    port = Port(run_result=_result())
    host, checkpoint, _, _, _, _, _ = _setup(
        tmp_path,
        port=port,
        unavailable={"linux-1"},
    )
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    host.submit(spec, now=NOW)

    waiting = host.prepare("work-1", now=NOW)

    assert waiting.state is BuildExecutionState.WAITING_FOR_NODE
    assert waiting.evidence is None
    assert "linux" in (waiting.block_reason or "")
    assert port.run_calls == 0
    assert (
        checkpoint.latest().snapshot.coordinator.records[0].state
        is BuildExecutionState.WAITING_FOR_NODE
    )


def test_existing_sqlite_state_requires_restore_before_any_new_effect(tmp_path) -> None:
    host, _, _, _, _, task_id, store = _setup(tmp_path)
    host.submit(_spec("work-1", Platform.LINUX, "linux-1"), now=NOW)

    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    restarted_port = Port(run_result=_result())
    restarted = DurableBuildExecutionHost(
        BuildExecutionCoordinator(
            registry,
            Available(set()),
            Authority(_authority("work-1", "linux-1")),
        ),
        restarted_port,
        FileEvidence(),
        OutputPolicies(
            BuildOutputPolicy(
                "project-1",
                "repo-main",
                "work-1",
                AllowedPathPolicy(("products/build",)),
                4,
                RepositoryPathIdentity.CASE_SENSITIVE,
            )
        ),
        SQLiteBuildExecutionCheckpointStore(store, task_id, "project-1"),
    )

    with pytest.raises(BuildExecutionDurabilityError, match="restore_latest"):
        restarted.prepare("work-1", now=NOW)
    assert restarted_port.run_calls == 0


def test_recomputed_tampered_checkpoint_cannot_change_project_identity(tmp_path) -> None:
    host, checkpoint, _, _, _, _, store = _setup(tmp_path)
    host.submit(_spec("work-1", Platform.LINUX, "linux-1"), now=NOW)
    saved = checkpoint.latest()

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = row["payload_json"].replace("project-1", "project-X")
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE checkpoints SET payload_json=?, checksum_sha256=? WHERE checkpoint_id=?",
            (payload, checksum, saved.checkpoint_id),
        )

    with pytest.raises(
        (BuildExecutionCheckpointIntegrityError, BuildExecutionDurabilityError),
    ):
        checkpoint.latest()
