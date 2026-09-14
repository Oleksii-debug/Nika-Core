from __future__ import annotations

import hashlib
import json
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

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "1" * 64
STAGE = "product_factory.build_execution.v1"


@dataclass
class Available:
    def is_available(self, node_id: str) -> bool:
        return True


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
class Files:
    def collect(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> tuple[ChangedFile, ...]:
        return ()


@dataclass
class CountingPort:
    run_calls: int = 0

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        return BuildExecutionResult(
            SHA,
            DIGEST,
            True,
            False,
            ("evidence://lineage",),
            NOW,
        )

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        return None


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity("linux-1", Platform.LINUX, "x86_64", "instance-linux-1"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(8, 16384, 65536),
    )


def _authority() -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        "work-1",
        frozenset({"build_release"}),
        ("linux-1",),
        ("products",),
        (),
        (),
        (ApprovedBuildCommand("build", ("python", "-m", "build")),),
        ("authority://lineage/1",),
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
            (),
            (),
            "build",
        ),
        120,
    )


def _make_host(tmp_path):
    store = SQLiteStore(tmp_path / "lineage.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    registry = ExecutionNodeRegistry()
    registry.register(_node())
    checkpoints = SQLiteBuildExecutionCheckpointStore(
        store,
        task.task_id,
        "project-1",
    )
    port = CountingPort()
    host = DurableBuildExecutionHost(
        BuildExecutionCoordinator(registry, Available(), Authority(_authority())),
        port,
        Files(),
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
        checkpoints,
    )
    return host, checkpoints, port, store, task.task_id


def _complete_once(host: DurableBuildExecutionHost) -> None:
    host.submit(_spec(), now=NOW)
    assert host.prepare("work-1", now=NOW).state is BuildExecutionState.PREPARED
    host.begin_dispatch("work-1", now=NOW)
    assert host.execute("work-1", now=NOW).state is BuildExecutionState.SUCCEEDED


def _prepared_row(store: SQLiteStore, task_id: str):
    with store.connection() as conn:
        return conn.execute(
            "SELECT checkpoint_id, payload_json, checksum_sha256, created_at "
            "FROM checkpoints WHERE task_id=? AND stage=? ORDER BY rowid LIMIT 1 OFFSET 1",
            (task_id, STAGE),
        ).fetchone()


def test_appended_stale_prepared_tail_is_rejected_before_restart(tmp_path) -> None:
    host, checkpoints, port, store, task_id = _make_host(tmp_path)
    _complete_once(host)
    assert port.run_calls == 1
    assert checkpoints.latest().snapshot.sequence == 5

    prepared = _prepared_row(store, task_id)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO checkpoints("
            "checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "pf5-forged-stale-tail",
                task_id,
                STAGE,
                prepared["payload_json"],
                prepared["checksum_sha256"],
                prepared["created_at"],
            ),
        )
        latest_row = conn.execute(
            "SELECT checkpoint_id FROM checkpoints "
            "WHERE task_id=? AND stage=? ORDER BY rowid DESC LIMIT 1",
            (task_id, STAGE),
        ).fetchone()
    assert latest_row["checkpoint_id"] == "pf5-forged-stale-tail"

    with pytest.raises(
        BuildExecutionDurabilityError,
        match="checkpoint identity|sequence history",
    ):
        checkpoints.latest()
    assert port.run_calls == 1


def test_fresh_sequence_cannot_semantically_rewind_succeeded_work_to_prepared(tmp_path) -> None:
    host, checkpoints, port, store, task_id = _make_host(tmp_path)
    _complete_once(host)
    assert port.run_calls == 1
    terminal = checkpoints.latest()
    assert terminal.snapshot.sequence == 5
    assert terminal.snapshot.coordinator.records[0].state is BuildExecutionState.SUCCEEDED

    prepared = _prepared_row(store, task_id)
    raw = json.loads(prepared["payload_json"])
    next_sequence = terminal.snapshot.sequence + 1
    raw["snapshot"]["fields"]["sequence"] = next_sequence
    forged_payload = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    checksum = hashlib.sha256(forged_payload.encode("utf-8")).hexdigest()
    checkpoint_id = "pf5-" + hashlib.sha256(
        f"{task_id}:{next_sequence}:{checksum}".encode()
    ).hexdigest()

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO checkpoints("
            "checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                checkpoint_id,
                task_id,
                STAGE,
                forged_payload,
                checksum,
                prepared["created_at"],
            ),
        )

    with pytest.raises(
        BuildExecutionDurabilityError,
        match="state transition",
    ):
        checkpoints.latest()
    assert port.run_calls == 1


def test_middle_checkpoint_deletion_is_rejected_as_sequence_gap(tmp_path) -> None:
    host, checkpoints, port, store, task_id = _make_host(tmp_path)
    _complete_once(host)
    assert checkpoints.latest().snapshot.sequence == 5

    with store.connection() as conn:
        middle = conn.execute(
            "SELECT checkpoint_id FROM checkpoints "
            "WHERE task_id=? AND stage=? ORDER BY rowid LIMIT 1 OFFSET 2",
            (task_id, STAGE),
        ).fetchone()
        conn.execute(
            "DELETE FROM checkpoints WHERE checkpoint_id=?",
            (middle["checkpoint_id"],),
        )

    with pytest.raises(BuildExecutionDurabilityError, match="sequence history"):
        checkpoints.latest()
    assert port.run_calls == 1


def test_deterministic_checkpoint_id_substitution_is_rejected(tmp_path) -> None:
    host, checkpoints, port, store, task_id = _make_host(tmp_path)
    _complete_once(host)
    terminal = checkpoints.latest()
    assert terminal.snapshot.sequence == 5

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id=? WHERE checkpoint_id=?",
            ("pf5-substituted-id", terminal.checkpoint_id),
        )

    with pytest.raises(BuildExecutionDurabilityError, match="checkpoint identity"):
        checkpoints.latest()
    assert port.run_calls == 1
