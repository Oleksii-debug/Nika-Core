from __future__ import annotations

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
    BuildExecutionResult,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
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


NOW = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
SHA = "a" * 40


@dataclass
class Available:
    def is_available(self, node_id: str) -> bool:
        return True


@dataclass
class Authority:
    value: ProjectExecutionAuthority

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> ProjectExecutionAuthority:
        return self.value


@dataclass
class OutputPolicies:
    value: BuildOutputPolicy

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> BuildOutputPolicy:
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
class NoEffectPort:
    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        raise AssertionError("restore test must not execute an external build effect")

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        raise AssertionError("restore test must not inspect an external build effect")


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity("linux-1", Platform.LINUX, "x86_64", "instance-linux-1"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(8, 16384, 65536),
    )


def _spec() -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project-1",
            "work-1",
            Platform.LINUX,
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(2, 2048, 4096),
        ),
        SHA,
        BuildExecutionScopeRequest(
            "repo-main",
            "products/build",
            ("linux-1",),
            ("pypi.org:443",),
            ("credref:package-index",),
            "build",
        ),
        120,
    )


def _authority(*, build_allowed: bool) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        "work-1",
        frozenset({"build_release"}) if build_allowed else frozenset({"run_tests"}),
        ("linux-1",),
        ("products",),
        ("pypi.org:443",),
        ("credref:package-index",),
        (ApprovedBuildCommand("build", ("python", "-m", "build")),),
        ("authority://trusted-plan/1",),
    )


def _policy() -> BuildOutputPolicy:
    return BuildOutputPolicy(
        "project-1",
        "repo-main",
        "work-1",
        AllowedPathPolicy(("products/build",)),
        4,
        RepositoryPathIdentity.CASE_SENSITIVE,
    )


def test_failed_restore_rolls_back_registry_after_temporary_lease_resurrection(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    checkpoint = SQLiteBuildExecutionCheckpointStore(store, task.task_id, "project-1")

    original_registry = ExecutionNodeRegistry()
    original_registry.register(_node())
    original_coordinator = BuildExecutionCoordinator(
        original_registry,
        Available(),
        Authority(_authority(build_allowed=True)),
    )
    original_host = DurableBuildExecutionHost(
        original_coordinator,
        NoEffectPort(),
        FileEvidence(),
        OutputPolicies(_policy()),
        checkpoint,
    )
    original_host.submit(_spec(), now=NOW)
    prepared = original_host.prepare("work-1", now=NOW)
    assert prepared.lease_id is not None
    assert checkpoint.latest().snapshot.leases

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.register(_node())
    registry_before = restarted_registry.snapshot()
    restarted_coordinator = BuildExecutionCoordinator(
        restarted_registry,
        Available(),
        Authority(_authority(build_allowed=False)),
    )
    restarted_host = DurableBuildExecutionHost(
        restarted_coordinator,
        NoEffectPort(),
        FileEvidence(),
        OutputPolicies(_policy()),
        checkpoint,
    )

    with pytest.raises(BuildExecutionError, match="no longer authorized"):
        restarted_host.restore_latest(now=NOW)

    assert restarted_registry.snapshot() == registry_before
    assert restarted_registry.snapshot().leases == ()
    with pytest.raises(BuildExecutionDurabilityError, match="restore_latest is required"):
        restarted_host.snapshot()
