from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    ChildRequest,
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


class _Definitions:
    def require_active(self, agent_id: str, agent_version: int) -> SimpleNamespace:
        assert agent_id in {"supervisor", "worker"}
        assert agent_version == 1
        return SimpleNamespace(definition=SimpleNamespace(tool_grants=()))


class _StoreBackedResolver:
    def __init__(self, store: MultiAgentStore) -> None:
        self._store = store

    def runtime_id_for_run(self, request: RuntimeRequest) -> str:
        return self._route(request.task_id, request.thread_id)

    def runtime_id_for_resume(self, request: RuntimeResumeRequest) -> str:
        return self._route(request.task_id, request.thread_id)

    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str:
        return self._route(task_id, thread_id)

    def _route(self, task_id: str, thread_id: str) -> str:
        prefix, team_id, member_id = task_id.split(":", 2)
        if prefix != "team":
            raise ValueError("unexpected task identity")
        member = self._store.member(team_id, member_id)
        if member.thread_id != thread_id:
            raise RuntimeError("thread does not match durable member")
        runtime_id = self._store.task_payload(team_id, member_id).get("runtime_id")
        if type(runtime_id) is not str:
            raise TypeError("durable task route must be exact text")
        return runtime_id


class _CursorObservingRuntime:
    def __init__(
        self,
        runtime_id: str,
        *,
        store: MultiAgentStore,
        durable_resume: bool,
    ) -> None:
        self._runtime_id = runtime_id
        self._store = store
        self._durable_resume = durable_resume
        self.initial_token_calls: list[tuple[str, str]] = []
        self.tokens_seen_before_effect: list[str | None] = []

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        values = {RuntimeCapability.PARALLELISM}
        if self._durable_resume:
            values.add(RuntimeCapability.DURABLE_RESUME)
        return frozenset(values)

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        if not self._durable_resume:
            raise AssertionError("non-durable route must not mint an initial cursor")
        self.initial_token_calls.append((task_id, thread_id))
        return f"cursor:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        _, team_id, member_id = request.task_id.split(":", 2)
        member = self._store.member(team_id, member_id)
        self.tokens_seen_before_effect.append(member.resume_token)
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False


def _store(tmp_path: Path) -> MultiAgentStore:
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
    return store


def _child(member_id: str, runtime_id: str) -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread-{member_id}",
        requested_grants=(),
        payload={"runtime_id": runtime_id},
    )


def test_m7_mixed_frozen_routes_bind_durable_cursor_before_child_effect(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    durable = _CursorObservingRuntime(
        "runtime-durable",
        store=store,
        durable_resume=True,
    )
    plain = _CursorObservingRuntime(
        "runtime-plain",
        store=store,
        durable_resume=False,
    )
    registry = RuntimeRegistry()
    registry.register(durable)
    registry.register(plain)
    router = FrozenRuntimeRouter(
        registry=registry,
        resolver=_StoreBackedResolver(store),
        allowed_runtime_ids=(durable.runtime_id, plain.runtime_id),
    )
    supervisor = MultiAgentSupervisor(
        runtime=router,
        store=store,
        definitions=_Definitions(),  # type: ignore[arg-type]
    )

    assert router.capabilities == frozenset({RuntimeCapability.PARALLELISM})
    assert RuntimeCapability.DURABLE_RESUME not in router.capabilities

    executions = asyncio.run(
        supervisor.fan_out(
            team_id="team-1",
            parent_id="root",
            requests=(
                _child("durable-child", durable.runtime_id),
                _child("plain-child", plain.runtime_id),
            ),
        )
    )

    assert [execution.member.state for execution in executions] == [
        MemberState.COMPLETED,
        MemberState.COMPLETED,
    ]
    assert durable.initial_token_calls == [
        ("team:team-1:durable-child", "thread-durable-child")
    ]
    assert plain.initial_token_calls == []
    assert durable.tokens_seen_before_effect == [
        "cursor:team:team-1:durable-child:thread-durable-child"
    ]
    assert plain.tokens_seen_before_effect == [None]
