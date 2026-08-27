from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.contracts import DeterministicAction
from nika_core.intelligence.runtime_effect_journal import RuntimeIdempotencyEffectJournal
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
from nika_core.runtime.idempotency import IdempotencyLedger


TEAM_ID = "team-v01-restart"
ROOT_ID = "supervisor"
WORKER_1 = "worker-1"
WORKER_2 = "worker-2"
CHECKER = "checker"


class SimulatedProcessLoss(BaseException):
    """Model abrupt application loss after the durable RUNNING boundary."""


class JournaledRestartRuntime(AgentRuntimePort):
    """Deterministic fixture that uses Nika's real durable effect journal."""

    def __init__(
        self,
        *,
        sqlite: SQLiteStore,
        external_effects: list[str],
        crash_members: frozenset[str] = frozenset(),
    ) -> None:
        self._journal = RuntimeIdempotencyEffectJournal(IdempotencyLedger(sqlite))
        self._external_effects = external_effects
        self._crash_members = crash_members
        self.run_requests: list[RuntimeRequest] = []
        self.resume_requests: list[RuntimeResumeRequest] = []
        self.cancelled: list[tuple[str, str]] = []

    @property
    def runtime_id(self) -> str:
        return "worker32-journaled-restart"

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
        self.run_requests.append(request)
        member_id = str(request.payload["member_id"])
        if member_id in self._crash_members:
            raise SimulatedProcessLoss(
                f"process lost after durable start for {member_id}"
            )
        if member_id != CHECKER:
            self._apply_external_effect(task_id=request.task_id, member_id=member_id)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output=self._output(member_id),
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_requests.append(request)
        member_id = request.task_id.rsplit(":", maxsplit=1)[-1]
        self._apply_external_effect(task_id=request.task_id, member_id=member_id)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output=self._output(member_id),
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancelled.append((task_id, thread_id))
        return True

    def probe_effect(self, *, member_id: str):
        return self._journal.reserve(
            task_id=self._task_id(member_id),
            action=self._effect_action(member_id),
        )

    def _apply_external_effect(self, *, task_id: str, member_id: str) -> None:
        reservation = self._journal.reserve(
            task_id=task_id,
            action=self._effect_action(member_id),
        )
        if reservation.created:
            self._external_effects.append(member_id)
            self._journal.complete(reservation.operation_key)
            return
        if reservation.status.value == "completed":
            return
        raise RuntimeError(
            f"effect for {member_id} requires reconciliation: {reservation.status.value}"
        )

    @staticmethod
    def _effect_action(member_id: str) -> DeterministicAction:
        return DeterministicAction(
            action_id=f"fixture-effect:{member_id}",
            adds=frozenset({f"effect-complete:{member_id}"}),
            tool_id="fixture.external-effect",
            arguments={"member_id": member_id},
        )

    @staticmethod
    def _output(member_id: str) -> dict[str, object]:
        if member_id == CHECKER:
            return {
                "member_id": member_id,
                "summary": "both worker prerequisites completed",
            }
        return {"member_id": member_id, "result": f"result:{member_id}"}

    @staticmethod
    def _task_id(member_id: str) -> str:
        return f"team:{TEAM_ID}:{member_id}"


def _sqlite(path: Path) -> SQLiteStore:
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    return sqlite


def _definitions(sqlite: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    compiler = AgentCompiler(tools=(), model_profiles={"test"})
    for agent_id in ("supervisor", "worker", "checker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            version=1,
            name=agent_id,
            goal="Complete the bounded V0.1 team assignment.",
            instructions="Preserve durable identity and return structured evidence.",
            model_profile="test",
            tool_grants=(),
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _create_team(store: MultiAgentStore) -> None:
    store.create_team(
        team_id=TEAM_ID,
        root_member_id=ROOT_ID,
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-supervisor",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=4,
            max_total_agents=6,
            max_parallel=2,
        ),
    )


def _request(member_id: str, *, checker: bool = False) -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="checker" if checker else "worker",
        agent_version=1,
        thread_id=f"thread-{member_id}",
        requested_grants=(),
        payload={"assignment": member_id},
    )


def _result_counts(sqlite: SQLiteStore) -> dict[str, int]:
    with sqlite.connection() as conn:
        rows = conn.execute(
            "SELECT member_id, COUNT(*) AS count FROM multi_agent_results "
            "WHERE team_id = ? GROUP BY member_id ORDER BY member_id",
            (TEAM_ID,),
        ).fetchall()
    return {str(row["member_id"]): int(row["count"]) for row in rows}


def _build_interrupted_team(
    path: Path,
) -> tuple[SQLiteStore, MultiAgentStore, list[str]]:
    sqlite = _sqlite(path)
    store = MultiAgentStore(sqlite)
    definitions = _definitions(sqlite)
    _create_team(store)
    external_effects: list[str] = []
    runtime = JournaledRestartRuntime(
        sqlite=sqlite,
        external_effects=external_effects,
        crash_members=frozenset({WORKER_2}),
    )
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=definitions,
    )

    worker_1 = asyncio.run(
        supervisor.fan_out(
            team_id=TEAM_ID,
            parent_id=ROOT_ID,
            requests=(_request(WORKER_1),),
        )
    )
    assert worker_1[0].member.state is MemberState.COMPLETED
    assert external_effects == [WORKER_1]

    with pytest.raises(SimulatedProcessLoss):
        asyncio.run(
            supervisor.fan_out(
                team_id=TEAM_ID,
                parent_id=ROOT_ID,
                requests=(_request(WORKER_2),),
            )
        )

    assert store.member(TEAM_ID, WORKER_1).state is MemberState.COMPLETED
    assert store.member(TEAM_ID, WORKER_2).state is MemberState.RUNNING
    assert store.member(TEAM_ID, WORKER_2).resume_token == f"thread-{WORKER_2}"
    assert {member.member_id for member in store.members(TEAM_ID)} == {
        ROOT_ID,
        WORKER_1,
        WORKER_2,
    }
    assert _result_counts(sqlite) == {WORKER_1: 1}
    return sqlite, store, external_effects


def test_restart_retains_completed_worker_and_resumes_only_running_worker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "three-agent-restart.db"
    initial_sqlite, _, external_effects = _build_interrupted_team(path)

    worker_1_records = IdempotencyLedger(initial_sqlite).list_for_task(
        f"team:{TEAM_ID}:{WORKER_1}"
    )
    assert len(worker_1_records) == 1
    assert worker_1_records[0].status.value == "completed"

    restarted_sqlite = _sqlite(path)
    restarted_store = MultiAgentStore(restarted_sqlite)
    restarted_runtime = JournaledRestartRuntime(
        sqlite=restarted_sqlite,
        external_effects=external_effects,
    )
    restarted = MultiAgentSupervisor(
        runtime=restarted_runtime,
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_sqlite),
    )

    replay_probe = restarted_runtime.probe_effect(member_id=WORKER_1)
    assert replay_probe.created is False
    assert replay_probe.status.value == "completed"
    assert external_effects == [WORKER_1]

    recovered = asyncio.run(restarted.recover_team(TEAM_ID))
    assert [item.member.member_id for item in recovered] == [WORKER_2]
    assert restarted_runtime.run_requests == []
    assert len(restarted_runtime.resume_requests) == 1
    assert restarted_runtime.resume_requests[0].task_id == f"team:{TEAM_ID}:{WORKER_2}"
    assert restarted_store.member(TEAM_ID, WORKER_1).state is MemberState.COMPLETED
    assert restarted_store.member(TEAM_ID, WORKER_2).state is MemberState.COMPLETED
    assert external_effects == [WORKER_1, WORKER_2]
    assert _result_counts(restarted_sqlite) == {WORKER_1: 1, WORKER_2: 1}

    checker = asyncio.run(
        restarted.fan_out(
            team_id=TEAM_ID,
            parent_id=ROOT_ID,
            requests=(_request(CHECKER, checker=True),),
        )
    )
    assert checker[0].member.state is MemberState.COMPLETED
    assert external_effects == [WORKER_1, WORKER_2]
    assert _result_counts(restarted_sqlite) == {
        CHECKER: 1,
        WORKER_1: 1,
        WORKER_2: 1,
    }

    assert restarted.finalize_team(TEAM_ID) is TeamState.COMPLETED
    assert restarted.finalize_team(TEAM_ID) is TeamState.COMPLETED
    assert restarted_store.team_state(TEAM_ID) is TeamState.COMPLETED

    events = AuditLog(restarted_sqlite).list_for(
        entity_type="multi_agent_team",
        entity_id=TEAM_ID,
    )
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)
    event_types = [event.event_type for event in events]
    assert event_types == [
        "multi_agent.team_created",
        "multi_agent.child_spawned",
        "multi_agent.child_execution_started",
        "multi_agent.child_execution_finished",
        "multi_agent.child_spawned",
        "multi_agent.child_execution_started",
        "multi_agent.child_execution_finished",
        "multi_agent.child_spawned",
        "multi_agent.child_execution_started",
        "multi_agent.child_execution_finished",
        "multi_agent.team_finalized",
    ]
    finished = [
        str(event.payload["member_id"])
        for event in events
        if event.event_type == "multi_agent.child_execution_finished"
    ]
    assert finished == [WORKER_1, WORKER_2, CHECKER]
    assert event_types.count("multi_agent.team_finalized") == 1


def test_checker_cannot_start_before_both_worker_prerequisites_are_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "checker-prerequisite.db"
    sqlite, store, external_effects = _build_interrupted_team(path)
    runtime = JournaledRestartRuntime(sqlite=sqlite, external_effects=external_effects)
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=AgentDefinitionRepository(sqlite),
    )

    try:
        asyncio.run(
            supervisor.fan_out(
                team_id=TEAM_ID,
                parent_id=ROOT_ID,
                requests=(_request(CHECKER, checker=True),),
            )
        )
    except RuntimeError:
        pass

    members = {member.member_id: member.state for member in store.members(TEAM_ID)}
    assert CHECKER not in members, (
        "V01-B02 defect: checker was allowed to start while worker-2 was still RUNNING; "
        "the integrated team path has no durable prerequisite gate"
    )
