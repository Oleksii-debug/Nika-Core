from __future__ import annotations

from pathlib import Path

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    HandoffKind,
    MemberState,
    MultiAgentStore,
    TeamQuota,
)


def _store(db_path: Path) -> MultiAgentStore:
    sqlite = SQLiteStore(db_path)
    sqlite.initialize()
    return MultiAgentStore(sqlite)


def _result_handoff(team_id: str, worker_id: str) -> AgentHandoff:
    return AgentHandoff(
        team_id=team_id,
        sender_id=worker_id,
        recipient_id="checker",
        kind=HandoffKind.RESULT,
        payload={"worker": worker_id, "evidence": f"evidence:{worker_id}"},
        handoff_id=f"result:{team_id}:{worker_id}",
        correlation_id=f"team:{team_id}:checker:{worker_id}",
    )


def test_two_terminal_worker_handoffs_survive_close_reopen_for_checker(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nika.db"
    store = _store(db_path)
    grant = ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",))
    store.create_team(
        team_id="team-restart",
        root_member_id="checker",
        root_agent_id="checker-agent",
        root_agent_version=1,
        root_thread_id="thread:checker",
        root_grants=(grant,),
        quota=TeamQuota(
            max_depth=1,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
        root_task_handoff=AgentHandoff(
            team_id="team-restart",
            sender_id="checker",
            recipient_id="checker",
            kind=HandoffKind.TASK,
            payload={"task_id": "task-restart"},
            handoff_id="task:checker",
        ),
    )
    for worker_id in ("worker-a", "worker-b"):
        store.spawn_child(
            team_id="team-restart",
            parent_id="checker",
            child_id=worker_id,
            agent_id=f"{worker_id}-agent",
            agent_version=1,
            thread_id=f"thread:{worker_id}",
            requested_grants=(grant,),
            task_handoff=AgentHandoff(
                team_id="team-restart",
                sender_id="checker",
                recipient_id=worker_id,
                kind=HandoffKind.TASK,
                payload={"task_id": "task-restart", "worker_id": worker_id},
                handoff_id=f"task:{worker_id}",
            ),
        )
        store.prepare_member_execution(
            team_id="team-restart",
            member_id=worker_id,
            resume_token=f"resume:{worker_id}",
        )
        store.finish_member_execution(
            team_id="team-restart",
            member_id=worker_id,
            state=MemberState.COMPLETED,
            outcome="completed",
            payload={"worker": worker_id, "evidence": f"evidence:{worker_id}"},
            result_handoff=_result_handoff("team-restart", worker_id),
        )

    restarted = _store(db_path)
    assert restarted.recoverable_children("team-restart") == ()
    handoffs = restarted.inbound_result_handoffs("team-restart", "checker")
    assert [handoff.sender_id for handoff in handoffs] == ["worker-a", "worker-b"]
    assert [handoff.payload["evidence"] for handoff in handoffs] == [
        "evidence:worker-a",
        "evidence:worker-b",
    ]
    assert all(handoff.kind is HandoffKind.RESULT for handoff in handoffs)
