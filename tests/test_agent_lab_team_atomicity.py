from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    HandoffKind,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
)


class _NoopCancelRuntime:
    capabilities = frozenset()

    async def cancel(self, *, task_id: str, thread_id: str) -> None:
        del task_id, thread_id


class _UnusedDefinitions:
    pass


def _store(tmp_path: Path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "atomic-team.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    grants = (ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),)
    store.create_team(
        team_id="team-atomic",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=grants,
        quota=TeamQuota(max_depth=2, max_children_per_parent=2, max_total_agents=4),
    )
    store.spawn_child(
        team_id="team-atomic",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=grants,
        task_handoff=AgentHandoff(
            team_id="team-atomic",
            sender_id="root",
            recipient_id="child",
            kind=HandoffKind.TASK,
            payload={"work": "atomic"},
        ),
    )
    store.prepare_member_execution(
        team_id="team-atomic",
        member_id="child",
        resume_token="thread-child",
    )
    return sqlite, store


def _result_handoff(*, handoff_id: str) -> AgentHandoff:
    return AgentHandoff(
        team_id="team-atomic",
        sender_id="child",
        recipient_id="root",
        kind=HandoffKind.RESULT,
        payload={"ok": True},
        handoff_id=handoff_id,
    )


def test_result_handoff_failure_rolls_back_result_and_member_state(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    store.record_handoff(_result_handoff(handoff_id="duplicate-result-handoff"))

    with pytest.raises(sqlite3.IntegrityError):
        store.finish_member_execution(
            team_id="team-atomic",
            member_id="child",
            state=MemberState.COMPLETED,
            outcome="completed",
            payload={"ok": True},
            result_handoff=_result_handoff(handoff_id="duplicate-result-handoff"),
        )

    member = store.member("team-atomic", "child")
    assert member.state is MemberState.RUNNING
    assert member.resume_token == "thread-child"
    with sqlite.connection() as conn:
        result_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM multi_agent_results "
                "WHERE team_id = ? AND member_id = ?",
                ("team-atomic", "child"),
            ).fetchone()[0]
        )
    assert result_count == 0


def test_late_runtime_result_cannot_overwrite_team_cancellation(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    supervisor = MultiAgentSupervisor(
        runtime=_NoopCancelRuntime(),
        store=store,
        definitions=_UnusedDefinitions(),
    )
    asyncio.run(supervisor.cancel_team("team-atomic"))

    member = store.finish_member_execution(
        team_id="team-atomic",
        member_id="child",
        state=MemberState.COMPLETED,
        outcome="completed",
        payload={"late": True},
        result_handoff=_result_handoff(handoff_id="late-result"),
    )

    assert member.state is MemberState.CANCELLED
    with sqlite.connection() as conn:
        result_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM multi_agent_results "
                "WHERE team_id = ? AND member_id = ?",
                ("team-atomic", "child"),
            ).fetchone()[0]
        )
    assert result_count == 0
