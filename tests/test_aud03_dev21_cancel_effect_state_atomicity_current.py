from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MultiAgentStore, MultiAgentSupervisor, TeamQuota, TeamState


class _CancelEffectThenErrorRuntime:
    capabilities = frozenset()

    def __init__(self) -> None:
        self.cancel_effects: list[str] = []

    async def cancel(self, *, task_id: str, thread_id: str) -> None:
        del thread_id
        self.cancel_effects.append(task_id)
        if len(self.cancel_effects) == 2:
            raise RuntimeError("uncertain cancellation result after external effect")


class _UnusedDefinitions:
    pass


def _store(tmp_path: Path) -> MultiAgentStore:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-aud03-cancel",
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
    store.spawn_child(
        team_id="team-aud03-cancel",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(),
    )
    return store


def test_external_cancel_effect_cannot_leave_restart_authority_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runtime = _CancelEffectThenErrorRuntime()
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=_UnusedDefinitions(),
    )

    with pytest.raises(RuntimeError, match="uncertain cancellation"):
        asyncio.run(supervisor.cancel_team("team-aud03-cancel"))

    assert len(runtime.cancel_effects) == 2
    assert store.team_state("team-aud03-cancel") is not TeamState.ACTIVE
