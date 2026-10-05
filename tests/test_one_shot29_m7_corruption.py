from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MultiAgentStore, TeamQuota
from nika_core.multi_agent.cancellation import TeamCancellationJournal


def _store(tmp_path: Path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "one-shot29-m7.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-corrupt",
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
        team_id="team-corrupt",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(),
    )
    return sqlite, store


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_depth", True),
        ("max_children_per_parent", "2"),
        ("max_total_agents", 3.0),
        ("max_parallel", False),
    ],
)
def test_team_quota_rejects_bool_string_and_float_numeric_coercion(
    field: str, value: object
) -> None:
    data: dict[str, object] = {
        "max_depth": 2,
        "max_children_per_parent": 2,
        "max_total_agents": 3,
        "max_parallel": 2,
    }
    data[field] = value
    with pytest.raises(TypeError, match="must be an integer"):
        TeamQuota(**data)  # type: ignore[arg-type]


def test_persisted_quota_corruption_fails_closed_on_restart_read(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_teams SET quota_json = ? WHERE team_id = ?",
            (
                json.dumps(
                    {
                        "max_depth": 2,
                        "max_children_per_parent": 2,
                        "max_total_agents": 3,
                        "max_parallel": True,
                    }
                ),
                "team-corrupt",
            ),
        )

    with pytest.raises(TypeError, match="max_parallel must be an integer"):
        store.quota("team-corrupt")


def test_deleted_cancel_effect_is_detected_after_restart(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    journal = TeamCancellationJournal(store)
    operation = journal.begin(team_id="team-corrupt")
    assert operation.expected_effect_count == 2

    with sqlite.connection() as conn:
        conn.execute(
            "DELETE FROM multi_agent_cancellation_effects "
            "WHERE operation_id = ? AND sequence = ?",
            (operation.operation_id, 1),
        )

    restarted = TeamCancellationJournal(store)
    with pytest.raises(RuntimeError, match="effect count is corrupt"):
        restarted.get("team-corrupt")


def test_cancel_effect_sequence_gap_is_detected_after_restart(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    journal = TeamCancellationJournal(store)
    operation = journal.begin(team_id="team-corrupt")

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_cancellation_effects SET sequence = ? "
            "WHERE operation_id = ? AND sequence = ?",
            (7, operation.operation_id, 1),
        )

    restarted = TeamCancellationJournal(store)
    with pytest.raises(RuntimeError, match="effect sequence is corrupt"):
        restarted.get("team-corrupt")


def test_cancel_expected_count_type_corruption_is_detected_after_restart(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    journal = TeamCancellationJournal(store)
    operation = journal.begin(team_id="team-corrupt")

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_cancellations SET expected_effect_count = ? "
            "WHERE operation_id = ?",
            ("two", operation.operation_id),
        )

    restarted = TeamCancellationJournal(store)
    with pytest.raises(TypeError, match="expected effect count is corrupt"):
        restarted.get("team-corrupt")


def test_cancelled_ledger_cannot_be_replayed_as_active_team(tmp_path: Path) -> None:
    sqlite, store = _store(tmp_path)
    journal = TeamCancellationJournal(store)
    journal.begin(team_id="team-corrupt")

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_teams SET state = 'active' WHERE team_id = ?",
            ("team-corrupt",),
        )

    restarted = TeamCancellationJournal(store)
    with pytest.raises(RuntimeError, match="team state is corrupt"):
        restarted.get("team-corrupt")
