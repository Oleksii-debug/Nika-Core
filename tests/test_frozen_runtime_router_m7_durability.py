from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    ChildRequest,
    HandoffKind,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
)
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.runtime.frozen_router import FrozenRuntimeRouter
from nika_core.runtime.registry import RuntimeRegistry


@dataclass
class _Overlap:
    active: int = 0
    max_active: int = 0


class _ModelRuntime:
    def __init__(self, runtime_id: str, *, overlap: _Overlap | None = None) -> None:
        self._runtime_id = runtime_id
        self._overlap = overlap
        self.run_calls: list[tuple[str, str]] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        # ModelGateway-style inference can be parallel without pretending that an
        # interrupted provider call has a provider-level durable resume cursor.
        return frozenset({RuntimeCapability.PARALLELISM})

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls.append((request.task_id, request.thread_id))
        if self._overlap is not None:
            self._overlap.active += 1
            self._overlap.max_active = max(self._overlap.max_active, self._overlap.active)
            try:
                await asyncio.sleep(0.02)
            finally:
                self._overlap.active -= 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={
                "runtime_id": self._runtime_id,
                "task_id": request.task_id,
                "thread_id": request.thread_id,
            },
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls.append((request.task_id, request.thread_id))
        raise AssertionError("non-resumable model runtime must not be resumed")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancel_calls.append((task_id, thread_id))
        return False


class _StoreBackedRouteResolver:
    """Resolve child route identity only from the canonical persisted TASK handoff."""

    def __init__(self, store: MultiAgentStore) -> None:
        self._store = store

    def runtime_id_for_run(self, request: RuntimeRequest) -> str:
        return self._route_for_task(request.task_id, request.thread_id)

    def runtime_id_for_resume(self, request: RuntimeResumeRequest) -> str:
        return self._route_for_task(request.task_id, request.thread_id)

    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str:
        return self._route_for_task(task_id, thread_id)

    def _route_for_task(self, task_id: str, thread_id: str) -> str:
        parts = task_id.split(":", 2)
        if len(parts) != 3 or parts[0] != "team":
            raise ValueError("M7 task identity must be team:<team_id>:<member_id>")
        _, team_id, member_id = parts
        member = self._store.member(team_id, member_id)
        if member.thread_id != thread_id:
            raise RuntimeError("runtime route lookup does not match durable child thread")
        payload = self._store.task_payload(team_id, member_id)
        runtime_id = payload.get("runtime_id")
        if type(runtime_id) is not str or not runtime_id or runtime_id != runtime_id.strip():
            raise TypeError("durable TASK handoff must contain an exact runtime_id")
        return runtime_id


def _sqlite_and_store(tmp_path: Path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-1",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=4,
            max_total_agents=6,
            max_parallel=2,
        ),
    )
    return sqlite, store


def _definitions(sqlite: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    compiler = AgentCompiler(tools=(), model_profiles={"test"})
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            version=1,
            name=agent_id,
            goal="Complete the assigned durable team task.",
            instructions="Use only the frozen route selected by the durable task handoff.",
            model_profile="test",
            tool_grants=(),
            enabled=True,
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _router(
    store: MultiAgentStore,
    *runtimes: _ModelRuntime,
) -> FrozenRuntimeRouter:
    registry = RuntimeRegistry()
    for runtime in runtimes:
        registry.register(runtime)
    return FrozenRuntimeRouter(
        registry=registry,
        resolver=_StoreBackedRouteResolver(store),
        allowed_runtime_ids=tuple(runtime.runtime_id for runtime in runtimes),
    )


def _child(member_id: str, runtime_id: str) -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread-{member_id}",
        requested_grants=(),
        payload={"runtime_id": runtime_id, "work": member_id},
    )


def test_m7_fanout_persists_distinct_frozen_routes_and_restart_does_not_rerun(
    tmp_path: Path,
) -> None:
    sqlite, store = _sqlite_and_store(tmp_path)
    definitions = _definitions(sqlite)
    overlap = _Overlap()
    runtime_a = _ModelRuntime("model-gateway:ollama-fast", overlap=overlap)
    runtime_b = _ModelRuntime("model-gateway:ollama-review", overlap=overlap)
    supervisor = MultiAgentSupervisor(
        runtime=_router(store, runtime_a, runtime_b),
        store=store,
        definitions=definitions,
    )

    executions = asyncio.run(
        supervisor.fan_out(
            team_id="team-1",
            parent_id="root",
            requests=(
                _child("worker-a", runtime_a.runtime_id),
                _child("worker-b", runtime_b.runtime_id),
            ),
        )
    )

    assert overlap.max_active == 2
    assert [execution.member.state for execution in executions] == [
        MemberState.COMPLETED,
        MemberState.COMPLETED,
    ]
    assert runtime_a.run_calls == [("team:team-1:worker-a", "thread-worker-a")]
    assert runtime_b.run_calls == [("team:team-1:worker-b", "thread-worker-b")]
    assert store.task_payload("team-1", "worker-a")["runtime_id"] == runtime_a.runtime_id
    assert store.task_payload("team-1", "worker-b")["runtime_id"] == runtime_b.runtime_id
    assert store.member_result("team-1", "worker-a").payload["runtime_id"] == runtime_a.runtime_id
    assert store.member_result("team-1", "worker-b").payload["runtime_id"] == runtime_b.runtime_id

    # Full reconstruction: no process-local route affinity or prior runtime object
    # is reused. Terminal children are not recoverable and must not be inferred again.
    restarted_sqlite = SQLiteStore(sqlite.path)
    restarted_store = MultiAgentStore(restarted_sqlite)
    restarted_a = _ModelRuntime(runtime_a.runtime_id)
    restarted_b = _ModelRuntime(runtime_b.runtime_id)
    restarted_supervisor = MultiAgentSupervisor(
        runtime=_router(restarted_store, restarted_a, restarted_b),
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_sqlite),
    )

    recovered = asyncio.run(restarted_supervisor.recover_team("team-1"))

    assert recovered == ()
    assert restarted_a.run_calls == []
    assert restarted_b.run_calls == []
    assert restarted_a.resume_calls == []
    assert restarted_b.resume_calls == []
    assert restarted_store.member_result("team-1", "worker-a").payload["runtime_id"] == runtime_a.runtime_id
    assert restarted_store.member_result("team-1", "worker-b").payload["runtime_id"] == runtime_b.runtime_id


def test_m7_restart_recovers_spawned_child_on_route_frozen_in_task_handoff(
    tmp_path: Path,
) -> None:
    sqlite, store = _sqlite_and_store(tmp_path)
    _definitions(sqlite)
    frozen_runtime_id = "model-gateway:ollama-review"
    store.spawn_child(
        team_id="team-1",
        parent_id="root",
        child_id="worker-pending",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-worker-pending",
        requested_grants=(),
        task_handoff=AgentHandoff(
            handoff_id="task:team-1:worker-pending",
            team_id="team-1",
            sender_id="root",
            recipient_id="worker-pending",
            kind=HandoffKind.TASK,
            correlation_id="team:team-1:root:worker-pending",
            payload={"runtime_id": frozen_runtime_id, "work": "pending"},
        ),
    )

    # Reconstruct every process-local object before recovery. Runtime A is present
    # and allowed, but the durable TASK handoff binds this child to runtime B.
    restarted_sqlite = SQLiteStore(sqlite.path)
    restarted_store = MultiAgentStore(restarted_sqlite)
    runtime_a = _ModelRuntime("model-gateway:ollama-fast")
    runtime_b = _ModelRuntime(frozen_runtime_id)
    restarted_supervisor = MultiAgentSupervisor(
        runtime=_router(restarted_store, runtime_a, runtime_b),
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_sqlite),
    )

    recovered = asyncio.run(restarted_supervisor.recover_team("team-1"))

    assert len(recovered) == 1
    assert recovered[0].member.member_id == "worker-pending"
    assert recovered[0].member.state is MemberState.COMPLETED
    assert runtime_a.run_calls == []
    assert runtime_b.run_calls == [
        ("team:team-1:worker-pending", "thread-worker-pending")
    ]
    assert restarted_store.task_payload("team-1", "worker-pending")["runtime_id"] == frozen_runtime_id
    assert restarted_store.member_result("team-1", "worker-pending").payload["runtime_id"] == frozen_runtime_id
