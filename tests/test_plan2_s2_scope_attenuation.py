"""Plan 2 Section 2: delegated tool scopes cannot expand parent authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MultiAgentStore, TeamQuota
from nika_core.multi_agent.contracts import attenuate_grants


@pytest.mark.parametrize(
    ("parent_scopes", "child_scopes", "allowed"),
    (
        ((), (), True),
        ((), ("repo:read",), True),
        (("repo:read", "repo:write"), ("repo:read",), True),
        (("repo:read",), ("repo:read",), True),
        (("repo:read",), (), False),
        (("repo:read",), ("other:read",), False),
    ),
)
def test_scope_attenuation_never_upgrades_a_constrained_parent(
    parent_scopes: tuple[str, ...],
    child_scopes: tuple[str, ...],
    allowed: bool,
) -> None:
    parent = (ToolGrant(tool_id="web.read", scopes=parent_scopes),)
    requested = (ToolGrant(tool_id="web.read", scopes=child_scopes),)
    if allowed:
        assert attenuate_grants(parent, requested) == requested
    else:
        with pytest.raises(PermissionError, match="broader scope"):
            attenuate_grants(parent, requested)


def test_rejected_unrestricted_child_creates_no_durable_member_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="scope-team",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(ToolGrant(tool_id="web.read", scopes=("repo:read",)),),
        quota=TeamQuota(max_depth=2, max_children_per_parent=2, max_total_agents=3),
    )
    with pytest.raises(PermissionError, match="broader scope"):
        store.spawn_child(
            team_id="scope-team",
            parent_id="root",
            child_id="unrestricted-child",
            agent_id="worker",
            agent_version=1,
            thread_id="thread-unrestricted",
            requested_grants=(ToolGrant(tool_id="web.read", scopes=()),),
        )
    restarted = MultiAgentStore(SQLiteStore(path))
    assert tuple(m.member_id for m in restarted.members("scope-team")) == ("root",)

    admitted = restarted.spawn_child(
        team_id="scope-team",
        parent_id="root",
        child_id="restricted-child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-restricted",
        requested_grants=(ToolGrant(tool_id="web.read", scopes=("repo:read",)),),
    )
    assert admitted.tool_grants[0].scopes == ("repo:read",)
    recovered = MultiAgentStore(SQLiteStore(path))
    assert recovered.member("scope-team", "restricted-child").tool_grants == (
        ToolGrant(tool_id="web.read", scopes=("repo:read",)),
    )
