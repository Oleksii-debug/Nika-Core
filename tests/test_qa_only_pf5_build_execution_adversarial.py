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
    BuildExecutionResult,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    BuildExecutionState,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
    BuildOutputPolicy,
    DurableBuildExecutionHost,
)
from nika_core.product_factory_build_execution_persistence import (
    BuildExecutionDurabilityError,
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

NOW = datetime(2026, 8, 23, 21, 30, tzinfo=UTC)
SHA = "a" * 40
ARTIFACT = "1" * 64
FILE_DIGEST = "2" * 64


@dataclass
class Availability:
    def is_available(self, node_id: str) -> bool:
        return True


@dataclass
class AuthorityPort:
    authority: ProjectExecutionAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.authority


@dataclass
class OutputPolicyPort:
    policy: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.policy


@dataclass
class FilePort:
    files: tuple[ChangedFile, ...]

    def collect(self, dispatch: BuildExecutionDispatch, result: BuildExecutionResult):
        return self.files


@dataclass
class NodePort:
    run_calls: int = 0
    inspect_calls: int = 0

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        return BuildExecutionResult(
            SHA,
            ARTIFACT,
            True,
            False,
            ("evidence://qa-build",),
            NOW,
        )

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        self.inspect_calls += 1
        return None


@dataclass
class FailOnEffectCheckpoint:
    delegate: SQLiteBuildExecutionCheckpointStore

    def has_checkpoint(self) -> bool:
        return self.delegate.has_checkpoint()

    def latest(self):
        return self.delegate.latest()

    def save(self, snapshot):
        if snapshot.coordinator.records[0].state is BuildExecutionState.EFFECT_IN_FLIGHT:
            raise RuntimeError("simulated durable write failure")
        return self.delegate.save(snapshot)


def _authority(work_id: str, node_id: str) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-qa",
        "repo-qa",
        work_id,
        frozenset({"build_release"}),
        (node_id,),
        ("products",),
        (),
        (),
        (ApprovedBuildCommand("build", ("python", "-m", "build")),),
        ("authority://qa/1",),
    )


def _spec(work_id: str, node_id: str) -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project-qa",
            work_id,
            Platform.LINUX,
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 1024),
        ),
        SHA,
        BuildExecutionScopeRequest(
            "repo-qa",
            "products/build",
            (node_id,),
            (),
            (),
            "build",
        ),
        120,
    )


def _make_host(
    tmp_path,
    *,
    files: tuple[ChangedFile, ...] = (),
    max_changed_files: int = 4,
    policy_work_id: str = "work-qa",
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-qa",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-qa"},
    )
    registry = ExecutionNodeRegistry()
    registry.register(
        ExecutionNode(
            NodeIdentity("linux-qa", Platform.LINUX, "x86_64", "instance-qa"),
            NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
            ResourceEnvelope(4, 4096, 8192),
        )
    )
    coordinator = BuildExecutionCoordinator(
        registry,
        Availability(),
        AuthorityPort(_authority("work-qa", "linux-qa")),
    )
    checkpoints = SQLiteBuildExecutionCheckpointStore(store, task.task_id, "project-qa")
    node_port = NodePort()
    output = OutputPolicyPort(
        BuildOutputPolicy(
            "project-qa",
            "repo-qa",
            policy_work_id,
            AllowedPathPolicy(("products/build",)),
            max_changed_files,
            RepositoryPathIdentity.CASE_SENSITIVE,
        )
    )
    host = DurableBuildExecutionHost(
        coordinator,
        node_port,
        FilePort(files),
        output,
        checkpoints,
    )
    return host, checkpoints, node_port, store, task.task_id


def _dispatch(host: DurableBuildExecutionHost) -> None:
    host.submit(_spec("work-qa", "linux-qa"), now=NOW)
    host.prepare("work-qa", now=NOW)
    host.begin_dispatch("work-qa", now=NOW)


@pytest.mark.parametrize(
    "entrypoint",
    (
        r"C:\Windows\System32\CMD.EXE",
        r"C:\Program Files\PowerShell\7\PwSh.ExE",
        "/usr/bin/BASH",
        "/bin/Sh",
        r"C:\Windows\System32\wSl.ExE",
    ),
)
def test_path_qualified_generic_shell_variants_are_rejected(entrypoint: str) -> None:
    with pytest.raises(ValueError, match="generic shell"):
        ApprovedBuildCommand("build", (entrypoint, "ignored"))


def test_sqlite_failure_at_effect_boundary_prevents_real_execution(tmp_path) -> None:
    host, checkpoints, node_port, _, _ = _make_host(tmp_path)
    host.checkpoints = FailOnEffectCheckpoint(checkpoints)  # type: ignore[assignment]
    _dispatch(host)

    with pytest.raises(RuntimeError, match="durable write failure"):
        host.execute("work-qa", now=NOW)

    assert node_port.run_calls == 0
    assert checkpoints.latest().snapshot.coordinator.records[0].state is BuildExecutionState.DISPATCHING
    with pytest.raises(BuildExecutionDurabilityError, match="persistence failed"):
        host.execute("work-qa", now=NOW)
    assert node_port.run_calls == 0


def test_changed_file_count_ceiling_cannot_be_bypassed_by_valid_paths(tmp_path) -> None:
    files = (
        ChangedFile("products/build/a.whl", FILE_DIGEST, 10),
        ChangedFile("products/build/b.whl", "3" * 64, 11),
    )
    host, checkpoints, node_port, _, _ = _make_host(
        tmp_path,
        files=files,
        max_changed_files=1,
    )
    _dispatch(host)

    record = host.execute("work-qa", now=NOW)

    assert node_port.run_calls == 1
    assert record.state is BuildExecutionState.RECONCILE_REQUIRED
    assert record.evidence is None
    assert checkpoints.latest().snapshot.file_evidence == ()


def test_substituted_output_policy_identity_cannot_authorize_terminal_evidence(tmp_path) -> None:
    files = (ChangedFile("products/build/a.whl", FILE_DIGEST, 10),)
    host, checkpoints, node_port, _, _ = _make_host(
        tmp_path,
        files=files,
        policy_work_id="other-work",
    )
    _dispatch(host)

    record = host.execute("work-qa", now=NOW)

    assert node_port.run_calls == 1
    assert record.state is BuildExecutionState.RECONCILE_REQUIRED
    assert checkpoints.latest().snapshot.file_evidence == ()


def test_recomputed_sqlite_checksum_cannot_widen_current_output_path_authority(tmp_path) -> None:
    files = (ChangedFile("products/build/good.whl", FILE_DIGEST, 10),)
    host, checkpoints, _, store, task_id = _make_host(tmp_path, files=files)
    _dispatch(host)
    assert host.execute("work-qa", now=NOW).state is BuildExecutionState.SUCCEEDED
    saved = checkpoints.latest()

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = row["payload_json"].replace(
            "products/build/good.whl",
            "other/evil.whl",
        )
        assert payload != row["payload_json"]
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE checkpoints SET payload_json=?, checksum_sha256=? WHERE checkpoint_id=?",
            (payload, checksum, saved.checkpoint_id),
        )

    registry = ExecutionNodeRegistry()
    registry.register(
        ExecutionNode(
            NodeIdentity("linux-qa", Platform.LINUX, "x86_64", "instance-qa"),
            NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
            ResourceEnvelope(4, 4096, 8192),
        )
    )
    restarted = DurableBuildExecutionHost(
        BuildExecutionCoordinator(
            registry,
            Availability(),
            AuthorityPort(_authority("work-qa", "linux-qa")),
        ),
        NodePort(),
        FilePort(()),
        OutputPolicyPort(
            BuildOutputPolicy(
                "project-qa",
                "repo-qa",
                "work-qa",
                AllowedPathPolicy(("products/build",)),
                4,
                RepositoryPathIdentity.CASE_SENSITIVE,
            )
        ),
        SQLiteBuildExecutionCheckpointStore(store, task_id, "project-qa"),
    )

    with pytest.raises(BuildExecutionDurabilityError, match="outside trusted output paths"):
        restarted.restore_latest(now=NOW)
