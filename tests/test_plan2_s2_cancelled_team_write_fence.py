"""Plan 2 Section 2: cancellation fences legacy handoff/result writer paths."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    CancellationReconciliationRequired,
    HandoffKind,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
    TeamState,
)
from nika_core.multi_agent.cancellation import TeamCancellationJournal


class _Runtime:
    capabilities = frozenset()

    def __init__(self, *, uncertain: bool) -> None:
        self.uncertain = uncertain
        self.effects: list[str] = []

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del thread_id
        self.effects.append(task_id)
        if self.uncertain and len(self.effects) == 2:
            raise RuntimeError("external cancellation result unknown")
        return True


def _store(path: Path) -> MultiAgentStore:
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="fenced-team",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="root-thread",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    store.spawn_child(
        team_id="fenced-team",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="child-thread",
        requested_grants=(),
    )
    return store


def _handoff(handoff_id: str) -> AgentHandoff:
    return AgentHandoff(
        team_id="fenced-team",
        sender_id="root",
        recipient_id="child",
        kind=HandoffKind.TASK,
        payload={"task": "fixture"},
        handoff_id=handoff_id,
    )


def _counts(path: Path) -> tuple[int, int]:
    with SQLiteStore(path).connection() as conn:
        handoffs = conn.execute(
            "SELECT COUNT(*) FROM multi_agent_handoffs WHERE team_id = ?",
            ("fenced-team",),
        ).fetchone()[0]
        results = conn.execute(
            "SELECT COUNT(*) FROM multi_agent_results WHERE team_id = ?",
            ("fenced-team",),
        ).fetchone()[0]
    return int(handoffs), int(results)


@pytest.mark.parametrize("uncertain", (False, True))
def test_late_direct_handoff_and_result_cannot_append_after_cancel(
    tmp_path: Path, uncertain: bool,
) -> None:
    path = tmp_path / "durable-team.db"
    store = _store(path)
    store.record_handoff(_handoff("before-cancel"))
    store.record_result(
        team_id="fenced-team", member_id="child", outcome="pre-cancel",
        payload={"allowed": True},
    )
    assert _counts(path) == (1, 1)

    runtime = _Runtime(uncertain=uncertain)
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=object())
    if uncertain:
        with pytest.raises(CancellationReconciliationRequired):
            asyncio.run(supervisor.cancel_team("fenced-team"))
    else:
        asyncio.run(supervisor.cancel_team("fenced-team"))
    assert store.team_state("fenced-team") is TeamState.CANCELLED

    # Both legacy paths must use the same writer-lock/terminal-state authority
    # as the supervisor; no late callback may create extra durable work.
    with pytest.raises(RuntimeError, match="team is not active"):
        store.record_handoff(_handoff("late-task"))
    with pytest.raises(RuntimeError, match="team is not active"):
        store.record_result(
            team_id="fenced-team", member_id="child",
            outcome="late-result", payload={"forged": True},
        )

    restarted = MultiAgentStore(SQLiteStore(path))
    assert restarted.team_state("fenced-team") is TeamState.CANCELLED
    assert _counts(path) == (1, 1)
    journal = TeamCancellationJournal(restarted).get("fenced-team")
    assert journal is not None
    assert journal.state.value == (
        "reconcile_required" if uncertain else "completed"
    )
    assert len(runtime.effects) == 2
