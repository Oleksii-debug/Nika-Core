from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MemberState, MultiAgentStore, MultiAgentSupervisor, TeamQuota
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult


def test_v01_b02_fresh_resume_timeout_and_paused_mapping_are_explicit() -> None:
    root_source = inspect.getsource(MultiAgentSupervisor._run_new_root)
    fresh_source = inspect.getsource(MultiAgentSupervisor._run_new_child)
    root_resume_source = inspect.getsource(MultiAgentSupervisor._recover_root)
    resume_source = inspect.getsource(MultiAgentSupervisor._recover_child)

    assert "timeout_seconds=" in root_source
    assert "timeout_seconds=" in fresh_source
    assert "timeout_seconds=" in root_resume_source
    assert "timeout_seconds=" in resume_source
    paused = MultiAgentSupervisor._state_for_result(
        RuntimeResult(outcome=RuntimeOutcome.PAUSED, resume_token="pause-token")
    )
    assert paused is MemberState.PAUSED
    assert paused.value == "paused"


def test_paused_member_is_durable_nonterminal_and_cancellable(tmp_path: Path) -> None:
    sqlite = SQLiteStore(tmp_path / "paused.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-paused",
        root_member_id="supervisor",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-supervisor",
        root_grants=(),
        quota=TeamQuota(
            max_depth=1,
            max_children_per_parent=1,
            max_total_agents=2,
            max_parallel=1,
        ),
    )
    store.spawn_child(
        team_id="team-paused",
        parent_id="supervisor",
        child_id="worker",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-worker",
        requested_grants=(),
    )
    store.set_member_state(
        team_id="team-paused",
        member_id="worker",
        state=MemberState.PAUSED,
        resume_token="pause-token",
    )

    paused = store.member("team-paused", "worker")
    assert paused.state is MemberState.PAUSED
    assert paused.resume_token == "pause-token"
    assert paused in store.recoverable_members("team-paused")
    with pytest.raises(RuntimeError, match="paused"):
        store.finalize_team("team-paused")

    # Re-running the canonical initializer must be idempotent and preserve the new state.
    sqlite.initialize()
    reopened = MultiAgentStore(sqlite)
    assert reopened.member("team-paused", "worker").state is MemberState.PAUSED

    cancelled = {member.member_id: member for member in reopened.cancel_team("team-paused")}
    assert cancelled["worker"].state is MemberState.CANCELLED
    assert cancelled["supervisor"].state is MemberState.CANCELLED
