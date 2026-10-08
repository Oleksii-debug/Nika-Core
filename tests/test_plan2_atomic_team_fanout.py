"""Plan 2 / Section 2: all-or-nothing durable team fan-out admission."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    ChildRequest,
    HandoffKind,
    MemberState,
    MultiAgentStore,
    TeamQuota,
)


def _store(path: Path) -> MultiAgentStore:
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-plan2",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(ToolGrant(tool_id="web.read", max_risk=0),),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    return store


def _request(member_id: str) -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread-{member_id}",
        requested_grants=(ToolGrant(tool_id="web.read", max_risk=0),),
        payload={"task": member_id},
    )


def _handoff(member_id: str, *, handoff_id: str | None = None) -> AgentHandoff:
    return AgentHandoff(
        team_id="team-plan2",
        sender_id="root",
        recipient_id=member_id,
        kind=HandoffKind.TASK,
        payload={"task": member_id},
        handoff_id=handoff_id or f"task:{member_id}",
    )


def _members(store: MultiAgentStore) -> list[str]:
    return [member.member_id for member in store.members("team-plan2")]


def test_quota_rejection_leaves_no_partial_work_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "quota.db"
    store = _store(path)
    store.spawn_child(
        team_id="team-plan2",
        parent_id="root",
        child_id="existing",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-existing",
        requested_grants=(),
    )
    with pytest.raises(RuntimeError, match="remaining children-per-parent quota"):
        store.spawn_children(
            team_id="team-plan2",
            parent_id="root",
            requests=(_request("first"), _request("second")),
        )
    assert _members(MultiAgentStore(SQLiteStore(path))) == ["root", "existing"]


def test_late_handoff_constraint_rejects_entire_wave(tmp_path: Path) -> None:
    store = _store(tmp_path / "handoff.db")
    with pytest.raises(sqlite3.IntegrityError):
        store.spawn_children(
            team_id="team-plan2",
            parent_id="root",
            requests=(_request("first"), _request("second")),
            task_handoffs=(
                _handoff("first", handoff_id="same-id"),
                _handoff("second", handoff_id="same-id"),
            ),
        )
    assert _members(store) == ["root"]


def test_duplicate_threads_and_untrusted_grants_cannot_partially_spawn(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "fail-closed.db")
    with pytest.raises(ValueError, match="unique within one fan-out batch"):
        store.spawn_children(
            team_id="team-plan2",
            parent_id="root",
            requests=(_request("same"), _request("same")),
        )
    escalated = ChildRequest(
        member_id="second",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-second",
        requested_grants=(ToolGrant(tool_id="release.publish", max_risk=4),),
    )
    with pytest.raises(PermissionError):
        store.spawn_children(
            team_id="team-plan2",
            parent_id="root",
            requests=(_request("first"), escalated),
        )
    assert _members(store) == ["root"]


def test_concurrent_fanout_waves_never_overbook_quota(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    store = _store(path)

    def wave(prefix: str) -> bool:
        try:
            store.spawn_children(
                team_id="team-plan2",
                parent_id="root",
                requests=(_request(prefix + "-a"), _request(prefix + "-b")),
            )
            return True
        except RuntimeError as exc:
            assert "quota" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(wave, ("left", "right")))
    assert sorted(results) == [False, True]
    assert len(_members(store)) == 3


@pytest.mark.parametrize(
    "terminal_state",
    (MemberState.COMPLETED, MemberState.FAILED, MemberState.CANCELLED),
)
def test_terminal_parent_cannot_delegate_after_restart(
    tmp_path: Path, terminal_state: MemberState,
) -> None:
    path = tmp_path / "terminal-parent.db"
    store = _store(path)
    store.finish_member_execution(
        team_id="team-plan2",
        member_id="root",
        state=terminal_state,
        outcome=terminal_state.value,
        payload={},
    )
    # Terminal member state persists even before an overall team-finalization step.
    restarted = MultiAgentStore(SQLiteStore(path))
    with pytest.raises(RuntimeError, match="terminal team member cannot delegate"):
        restarted.spawn_children(
            team_id="team-plan2",
            parent_id="root",
            requests=(_request("late"),),
            task_handoffs=(_handoff("late"),),
        )
    assert _members(MultiAgentStore(SQLiteStore(path))) == ["root"]
    with SQLiteStore(path).connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM multi_agent_handoffs WHERE team_id = ?",
            ("team-plan2",),
        ).fetchone()[0] == 0
