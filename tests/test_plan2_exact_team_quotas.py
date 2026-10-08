"""Plan 2 §2: team quota boundaries are exact integers across SQLite restart."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import ChildRequest, MultiAgentStore, TeamQuota


@pytest.mark.parametrize(
    ("name", "invalid"),
    (
        ("max_depth", True),
        ("max_depth", 2.0),
        ("max_children_per_parent", 1.5),
        ("max_children_per_parent", False),
        ("max_total_agents", 2.5),
        ("max_total_agents", "3"),
        ("max_parallel", 1.5),
        ("max_parallel", True),
    ),
)
def test_nonintegral_team_quota_rejected_before_admission(name: str, invalid: object) -> None:
    values: dict[str, object] = {
        "max_depth": 2,
        "max_children_per_parent": 2,
        "max_total_agents": 3,
        "max_parallel": 2,
    }
    values[name] = invalid
    with pytest.raises(TypeError, match=f"{name} must be an integer"):
        TeamQuota(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "invalid"),
    (
        ("max_parallel", 1.5),
        ("max_total_agents", 2.5),
        ("max_depth", True),
    ),
)
def test_corrupt_durable_quota_blocks_spawn_without_partial_member_write(
    tmp_path: Path, name: str, invalid: object,
) -> None:
    path = tmp_path / "team.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="typed-team",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    values: dict[str, object] = {
        "max_depth": 2,
        "max_children_per_parent": 2,
        "max_total_agents": 3,
        "max_parallel": 2,
    }
    values[name] = invalid
    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_teams SET quota_json = ? WHERE team_id = ?",
            (json.dumps(values), "typed-team"),
        )

    restarted = MultiAgentStore(SQLiteStore(path))
    with pytest.raises(TypeError, match=f"{name} must be an integer"):
        restarted.quota("typed-team")
    with pytest.raises(TypeError, match=f"{name} must be an integer"):
        restarted.spawn_children(
            team_id="typed-team",
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="child",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-child",
                ),
            ),
        )
    assert [item.member_id for item in restarted.members("typed-team")] == ["root"]


def test_integer_quota_preserves_normal_team_admission_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "valid-team.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="typed-team",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    restarted = MultiAgentStore(SQLiteStore(path))
    assert restarted.quota("typed-team").max_parallel == 2
    children = restarted.spawn_children(
        team_id="typed-team",
        parent_id="root",
        requests=(
            ChildRequest(
                member_id="child",
                agent_id="worker",
                agent_version=1,
                thread_id="thread-child",
            ),
        ),
    )
    assert len(children) == 1
    assert [item.member_id for item in restarted.members("typed-team")] == ["root", "child"]
