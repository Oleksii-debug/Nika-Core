from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.multi_agent import (
    ChildRequest,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
    TeamState,
)
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)


class _ImmediateRuntime(AgentRuntimePort):
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []
        self.resumed: list[RuntimeResumeRequest] = []

    @property
    def runtime_id(self) -> str:
        return "worker45-immediate"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset(
            {
                RuntimeCapability.DURABLE_RESUME,
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.PARALLELISM,
                RuntimeCapability.SUBAGENTS,
            }
        )

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"task_id": request.task_id},
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resumed.append(request)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancelled.append((task_id, thread_id))
        return True


class _BlockingRuntime(_ImmediateRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started: set[str] = set()
        self.two_children_started = asyncio.Event()
        self.release_children = asyncio.Event()
        self.root_cancel_entered = asyncio.Event()
        self.release_root_cancel = asyncio.Event()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.started.add(request.task_id)
        if len(self.started) >= 2:
            self.two_children_started.set()
        await self.release_children.wait()
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"task_id": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancelled.append((task_id, thread_id))
        if task_id == "team:team-1:root":
            self.root_cancel_entered.set()
            await self.release_root_cancel.wait()
        return True


class _UncertainCancelRuntime(_ImmediateRuntime):
    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancelled.append((task_id, thread_id))
        raise TimeoutError("cancellation transport outcome is unknown")


def _make_store(tmp_path: Path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    return sqlite, MultiAgentStore(sqlite)


def _definitions(sqlite: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    compiler = AgentCompiler(tools=(), model_profiles={"test"})
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            version=1,
            name=agent_id,
            goal="Complete the assigned team task.",
            instructions="Return deterministic structured evidence.",
            model_profile="test",
            tool_grants=(),
            enabled=True,
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _create_team(store: MultiAgentStore, *, team_id: str, root_id: str = "root") -> None:
    store.create_team(
        team_id=team_id,
        root_member_id=root_id,
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id=f"thread:{team_id}:{root_id}",
        root_grants=(),
        quota=TeamQuota(
            max_depth=1,
            max_children_per_parent=3,
            max_total_agents=4,
            max_parallel=2,
        ),
    )


def _request(member_id: str) -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread:team-1:{member_id}",
        requested_grants=(),
        payload={"member_id": member_id},
    )


def _spawn_running_child(
    store: MultiAgentStore,
    *,
    team_id: str,
    member_id: str,
    root_id: str = "root",
) -> None:
    store.spawn_child(
        team_id=team_id,
        parent_id=root_id,
        child_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread:{team_id}:{member_id}",
        requested_grants=(),
    )
    store.set_member_state(
        team_id=team_id,
        member_id=member_id,
        state=MemberState.RUNNING,
        resume_token=f"resume:{team_id}:{member_id}",
    )


def test_cancel_commits_authority_before_external_cleanup_and_blocks_new_assignment(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[TeamState, bool, tuple[MemberState, ...]]:
        sqlite, store = _make_store(tmp_path)
        definitions = _definitions(sqlite)
        _create_team(store, team_id="team-1")
        runtime = _BlockingRuntime()
        supervisor = MultiAgentSupervisor(
            runtime=runtime,
            store=store,
            definitions=definitions,
        )

        fanout_task = asyncio.create_task(
            supervisor.fan_out(
                team_id="team-1",
                parent_id="root",
                requests=(_request("worker-a"), _request("worker-b")),
            )
        )
        await asyncio.wait_for(runtime.two_children_started.wait(), timeout=1.0)

        cancel_task = asyncio.create_task(supervisor.cancel_team("team-1"))
        await asyncio.wait_for(runtime.root_cancel_entered.wait(), timeout=1.0)
        state_when_external_cleanup_started = store.team_state("team-1")

        late_assignment_succeeded = False
        try:
            store.spawn_child(
                team_id="team-1",
                parent_id="root",
                child_id="worker-late",
                agent_id="worker",
                agent_version=1,
                thread_id="thread:team-1:worker-late",
                requested_grants=(),
            )
            late_assignment_succeeded = True
        except RuntimeError:
            pass

        runtime.release_root_cancel.set()
        await asyncio.wait_for(cancel_task, timeout=1.0)
        runtime.release_children.set()
        await asyncio.wait_for(fanout_task, timeout=1.0)
        child_states = tuple(
            member.state
            for member in store.members("team-1")
            if member.parent_id is not None
        )
        return state_when_external_cleanup_started, late_assignment_succeeded, child_states

    state, late_assignment_succeeded, child_states = asyncio.run(scenario())

    assert state is TeamState.CANCELLED
    assert not late_assignment_succeeded
    assert child_states
    assert all(state is MemberState.CANCELLED for state in child_states)


def test_uncertain_cancel_cleanup_cannot_resurrect_children_or_fabricate_completion(
    tmp_path: Path,
) -> None:
    sqlite, store = _make_store(tmp_path)
    definitions = _definitions(sqlite)
    _create_team(store, team_id="team-1")
    _spawn_running_child(store, team_id="team-1", member_id="worker-a")
    _spawn_running_child(store, team_id="team-1", member_id="worker-b")
    supervisor = MultiAgentSupervisor(
        runtime=_UncertainCancelRuntime(),
        store=store,
        definitions=definitions,
    )

    try:
        asyncio.run(supervisor.cancel_team("team-1"))
    except TimeoutError:
        pass

    reloaded_sqlite = SQLiteStore(sqlite.path)
    reloaded_store = MultiAgentStore(reloaded_sqlite)
    resumed_runtime = _ImmediateRuntime()
    restarted = MultiAgentSupervisor(
        runtime=resumed_runtime,
        store=reloaded_store,
        definitions=AgentDefinitionRepository(reloaded_sqlite),
    )
    try:
        recovered = asyncio.run(restarted.recover_team("team-1"))
    except RuntimeError:
        recovered = ()
    final_state = restarted.finalize_team("team-1")

    assert reloaded_store.team_state("team-1") is TeamState.CANCELLED
    assert recovered == ()
    assert resumed_runtime.resumed == []
    assert final_state is TeamState.CANCELLED


def test_cancel_retains_completed_evidence_and_leaves_unrelated_team_untouched(
    tmp_path: Path,
) -> None:
    sqlite, store = _make_store(tmp_path)
    definitions = _definitions(sqlite)
    _create_team(store, team_id="team-1")
    completed = store.spawn_child(
        team_id="team-1",
        parent_id="root",
        child_id="worker-done",
        agent_id="worker",
        agent_version=1,
        thread_id="thread:team-1:worker-done",
        requested_grants=(),
    )
    store.prepare_member_execution(
        team_id="team-1",
        member_id=completed.member_id,
        resume_token="resume:done",
    )
    store.finish_member_execution(
        team_id="team-1",
        member_id=completed.member_id,
        state=MemberState.COMPLETED,
        outcome=RuntimeOutcome.COMPLETED.value,
        payload={"evidence": "retained"},
    )
    _spawn_running_child(store, team_id="team-1", member_id="worker-active")
    _create_team(store, team_id="team-2", root_id="other-root")

    runtime = _ImmediateRuntime()
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=definitions,
    )
    asyncio.run(supervisor.cancel_team("team-1"))

    reloaded = MultiAgentStore(SQLiteStore(sqlite.path))
    with sqlite.connection() as conn:
        row = conn.execute(
            "SELECT outcome, payload_json FROM multi_agent_results "
            "WHERE team_id = ? AND member_id = ?",
            ("team-1", "worker-done"),
        ).fetchone()

    assert reloaded.team_state("team-1") is TeamState.CANCELLED
    assert reloaded.member("team-1", "worker-done").state is MemberState.COMPLETED
    assert row is not None
    assert row["outcome"] == RuntimeOutcome.COMPLETED.value
    assert json.loads(row["payload_json"]) == {"evidence": "retained"}
    assert reloaded.team_state("team-2") is TeamState.ACTIVE
    assert all(not task_id.startswith("team:team-2:") for task_id, _ in runtime.cancelled)


def test_cancel_persists_a_visible_terminal_reason(tmp_path: Path) -> None:
    sqlite, store = _make_store(tmp_path)
    definitions = _definitions(sqlite)
    _create_team(store, team_id="team-1")
    supervisor = MultiAgentSupervisor(
        runtime=_ImmediateRuntime(),
        store=store,
        definitions=definitions,
    )

    asyncio.run(supervisor.cancel_team("team-1"))

    events = AuditLog(sqlite).list_for(entity_type="multi_agent_team", entity_id="team-1")
    cancellation = next(
        event for event in reversed(events) if event.event_type == "multi_agent.team_cancelled"
    )
    reason = next(
        (
            cancellation.payload.get(key)
            for key in ("reason", "terminal_reason", "cancellation_reason", "cause")
            if cancellation.payload.get(key) is not None
        ),
        None,
    )

    assert isinstance(reason, str)
    assert reason.strip()
