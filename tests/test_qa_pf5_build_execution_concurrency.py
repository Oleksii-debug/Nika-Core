from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionCoordinator,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
    BuildOutputPolicy,
    DurableBuildExecutionHost,
)
from nika_core.product_factory_build_execution_persistence import (
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
from nika_core.toolsmith.contracts import AllowedPathPolicy

NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
SHA = "a" * 40


@dataclass
class _Available:
    def is_available(self, node_id: str) -> bool:
        del node_id
        return True


@dataclass
class _Authorities:
    values: dict[str, ProjectExecutionAuthority]

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        assert project_id == "project:pf5-concurrency"
        assert repository_id == "repo:main"
        return self.values[work_id]


@dataclass
class _OutputPolicies:
    values: dict[str, BuildOutputPolicy]

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        assert project_id == "project:pf5-concurrency"
        assert repository_id == "repo:main"
        return self.values[work_id]


@dataclass
class _NoExternalEffects:
    def run(self, dispatch):
        raise AssertionError(f"unexpected external build effect: {dispatch.work_id}")

    def inspect(self, dispatch):
        raise AssertionError(f"unexpected external build inspection: {dispatch.work_id}")


@dataclass
class _NoFileEvidence:
    def collect(self, dispatch, result):
        raise AssertionError(
            f"unexpected build file evidence collection: {dispatch.work_id}:{result.source_sha}"
        )


@dataclass
class _BlockingFirstSaveStore:
    """Force two host mutations to contend at the first durable sequence boundary."""

    inner: SQLiteBuildExecutionCheckpointStore
    first_save_entered: Event = field(default_factory=Event)
    second_save_entered: Event = field(default_factory=Event)
    release_first_save: Event = field(default_factory=Event)
    _counter_lock: Lock = field(default_factory=Lock)
    _save_calls: int = 0

    def has_checkpoint(self) -> bool:
        return self.inner.has_checkpoint()

    def latest(self):
        return self.inner.latest()

    def save(self, snapshot):
        with self._counter_lock:
            self._save_calls += 1
            call_number = self._save_calls
        if call_number == 1:
            self.first_save_entered.set()
            if not self.release_first_save.wait(timeout=5):
                raise AssertionError("timed out releasing the first PF5 checkpoint save")
        elif call_number == 2:
            self.second_save_entered.set()
        return self.inner.save(snapshot)


def _node(node_id: str) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(node_id, Platform.LINUX, "x86_64", f"instance:{node_id}"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(8, 16384, 65536),
    )


def _spec(work_id: str, node_id: str) -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project:pf5-concurrency",
            work_id,
            Platform.LINUX,
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 1024, 2048),
        ),
        SHA,
        BuildExecutionScopeRequest(
            "repo:main",
            "products/app",
            (node_id,),
            (),
            (),
            "build",
        ),
        120,
    )


def _authority(work_id: str, node_id: str) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project:pf5-concurrency",
        "repo:main",
        work_id,
        frozenset({"build_release"}),
        (node_id,),
        ("products/app",),
        (),
        (),
        (ApprovedBuildCommand("build", ("python", "-m", "build")),),
        (f"authority://pf5-concurrency/{work_id}",),
    )


def _output_policy(work_id: str) -> BuildOutputPolicy:
    return BuildOutputPolicy(
        "project:pf5-concurrency",
        "repo:main",
        work_id,
        AllowedPathPolicy(("products/app",)),
        4,
        RepositoryPathIdentity.CASE_SENSITIVE,
    )


def test_concurrent_independent_submits_linearize_without_poisoning_or_lost_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pf5-concurrency.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws:pf5-concurrency",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": "project:pf5-concurrency",
        },
    )
    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-a"))
    registry.register(_node("linux-b"))
    authorities = _Authorities(
        {
            "work:a": _authority("work:a", "linux-a"),
            "work:b": _authority("work:b", "linux-b"),
        }
    )
    policies = _OutputPolicies(
        {
            "work:a": _output_policy("work:a"),
            "work:b": _output_policy("work:b"),
        }
    )
    durable_store = SQLiteBuildExecutionCheckpointStore(
        store,
        task.task_id,
        "project:pf5-concurrency",
    )
    blocking_store = _BlockingFirstSaveStore(durable_store)
    host = DurableBuildExecutionHost(
        BuildExecutionCoordinator(registry, _Available(), authorities),
        _NoExternalEffects(),
        _NoFileEvidence(),
        policies,
        blocking_store,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(host.submit, _spec("work:a", "linux-a"), now=NOW)
        assert blocking_store.first_save_entered.wait(timeout=5)

        second = pool.submit(host.submit, _spec("work:b", "linux-b"), now=NOW)
        second_reached_same_boundary = blocking_store.second_save_entered.wait(timeout=1)
        if second_reached_same_boundary:
            second.result(timeout=5)

        blocking_store.release_first_save.set()
        first_record = first.result(timeout=5)
        second_record = second.result(timeout=5)

    assert {first_record.spec.request.work_id, second_record.spec.request.work_id} == {
        "work:a",
        "work:b",
    }
    latest = durable_store.latest().snapshot
    assert latest.sequence == 2
    assert {record.spec.request.work_id for record in latest.coordinator.records} == {
        "work:a",
        "work:b",
    }
    assert host.snapshot() == latest
