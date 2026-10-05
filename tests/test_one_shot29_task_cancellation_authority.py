from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import ChildRequest, MultiAgentStore, MultiAgentSupervisor, TeamQuota, TeamState
from nika_core.multi_agent.cancellation import TeamCancellationJournal
from nika_core.multi_agent.contracts import (
    CancellationEffectState,
    CancellationOperationState,
    CancellationReconciliationRequired,
)


@dataclass(frozen=True, slots=True)
class _Definition:
    tool_grants: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class _StoredDefinition:
    definition: _Definition = _Definition()


class _Definitions:
    def require_active(self, agent_id: str, version: int) -> _StoredDefinition:
        del agent_id, version
        return _StoredDefinition()


class _BlockingRuntime:
    capabilities = frozenset()

    def __init__(self, store: MultiAgentStore, *, fail_second_cancel: bool = False) -> None:
        self._store = store
        self._fail_second_cancel = fail_second_cancel
        self.run_entered = asyncio.Event()
        self.resume_entered = asyncio.Event()
        self.cancel_effects: list[str] = []
        self.pre_effect_snapshots: list[tuple[TeamState, CancellationEffectState, str, str]] = []

    async def run(self, request: object) -> object:
        del request
        self.run_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def resume(self, request: object) -> object:
        del request
        self.resume_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cancel(self, *, task_id: str, thread_id: str) -> None:
        journal = TeamCancellationJournal(self._store)
        operation = journal.get("team-task-cancel")
        if operation is None:
            raise AssertionError("external cancellation dispatched without durable operation")
        effect = next((item for item in operation.effects if item.task_id == task_id), None)
        if effect is None:
            raise AssertionError(f"missing durable cancellation effect for {task_id}")
        self.pre_effect_snapshots.append(
            (
                self._store.team_state("team-task-cancel"),
                effect.state,
                effect.task_id,
                effect.thread_id,
            )
        )
        self.cancel_effects.append(task_id)
        if self._fail_second_cancel and len(self.cancel_effects) == 2:
            raise RuntimeError("uncertain cancellation after external effect")
        if effect.thread_id != thread_id:
            raise AssertionError("runtime cancellation thread identity differs from durable effect")


class _RecordingRuntime:
    capabilities = frozenset()

    def __init__(self) -> None:
        self.cancel_effects: list[str] = []
        self.resume_effects: list[str] = []

    async def cancel(self, *, task_id: str, thread_id: str) -> None:
        del thread_id
        self.cancel_effects.append(task_id)

    async def resume(self, request: object) -> object:
        self.resume_effects.append(type(request).__name__)
        raise AssertionError("restart must not resume an unfinished cancellation")


def _store(tmp_path: Path, *, with_child: bool) -> tuple[Path, MultiAgentStore]:
    path = tmp_path / "task-cancel.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-task-cancel",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=1,
            max_total_agents=2,
            max_parallel=1,
        ),
    )
    if with_child:
        store.spawn_child(
            team_id="team-task-cancel",
            parent_id="root",
            child_id="child",
            agent_id="worker",
            agent_version=1,
            thread_id="thread-child",
            requested_grants=(),
        )
    return path, store


async def _cancel_live_fanout(supervisor: MultiAgentSupervisor, runtime: _BlockingRuntime) -> None:
    task = asyncio.create_task(
        supervisor.fan_out(
            team_id="team-task-cancel",
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="child",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-child",
                    requested_grants=(),
                ),
            ),
        )
    )
    await runtime.run_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _cancel_recovery(supervisor: MultiAgentSupervisor, runtime: _BlockingRuntime) -> None:
    task = asyncio.create_task(supervisor.recover_team("team-task-cancel"))
    await runtime.resume_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_task_cancel_durably_commits_authority_before_runtime_cleanup(tmp_path: Path) -> None:
    _, store = _store(tmp_path, with_child=False)
    runtime = _BlockingRuntime(store)
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=_Definitions())

    asyncio.run(_cancel_live_fanout(supervisor, runtime))

    assert runtime.cancel_effects == [
        "team:team-task-cancel:root",
        "team:team-task-cancel:child",
    ]
    assert runtime.pre_effect_snapshots == [
        (
            TeamState.CANCELLED,
            CancellationEffectState.DISPATCHING,
            "team:team-task-cancel:root",
            "thread-root",
        ),
        (
            TeamState.CANCELLED,
            CancellationEffectState.DISPATCHING,
            "team:team-task-cancel:child",
            "thread-child",
        ),
    ]
    operation = TeamCancellationJournal(store).get("team-task-cancel")
    assert operation is not None
    assert operation.state is CancellationOperationState.COMPLETED
    assert all(effect.state is CancellationEffectState.CONFIRMED for effect in operation.effects)
    assert store.team_state("team-task-cancel") is TeamState.CANCELLED


def test_cancelled_recovery_uncertainty_is_durable_and_never_blindly_retried(
    tmp_path: Path,
) -> None:
    path, store = _store(tmp_path, with_child=True)
    store.prepare_member_execution(
        team_id="team-task-cancel",
        member_id="child",
        resume_token="resume-child-1",
    )
    runtime = _BlockingRuntime(store, fail_second_cancel=True)
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=_Definitions())

    asyncio.run(_cancel_recovery(supervisor, runtime))

    assert runtime.cancel_effects == [
        "team:team-task-cancel:root",
        "team:team-task-cancel:child",
    ]
    assert all(snapshot[0] is TeamState.CANCELLED for snapshot in runtime.pre_effect_snapshots)
    assert all(
        snapshot[1] is CancellationEffectState.DISPATCHING
        for snapshot in runtime.pre_effect_snapshots
    )
    operation = TeamCancellationJournal(store).get("team-task-cancel")
    assert operation is not None
    assert operation.state is CancellationOperationState.RECONCILE_REQUIRED
    assert [effect.state for effect in operation.effects] == [
        CancellationEffectState.CONFIRMED,
        CancellationEffectState.RECONCILE_REQUIRED,
    ]

    restarted = MultiAgentStore(SQLiteStore(path))
    retry_runtime = _RecordingRuntime()
    restarted_supervisor = MultiAgentSupervisor(
        runtime=retry_runtime,
        store=restarted,
        definitions=_Definitions(),
    )
    with pytest.raises(CancellationReconciliationRequired, match="unfinished durable cancellation"):
        asyncio.run(restarted_supervisor.recover_team("team-task-cancel"))
    assert retry_runtime.cancel_effects == []
    assert retry_runtime.resume_effects == []
