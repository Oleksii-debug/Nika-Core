from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nika_core.agent_lab_status import AgentLabStateProvider, AgentLabStatusReader
from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentSnapshot,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)
from nika_core.experiments.repository import SQLiteExperimentRepository
from nika_core.multi_agent.contracts import MemberState, TeamQuota
from nika_core.multi_agent.store import MultiAgentStore

_SECRET = "AGENT_LAB_SECRET_CANARY_7f4e27"


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _create_team(store: SQLiteStore, *, team_id: str = "team-alpha") -> None:
    teams = MultiAgentStore(store)
    teams.create_team(
        team_id=team_id,
        root_member_id="root",
        root_agent_id="agent.root",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(max_depth=3, max_children_per_parent=3, max_total_agents=8, max_parallel=2),
    )
    teams.spawn_child(
        team_id=team_id,
        parent_id="root",
        child_id="child-1",
        agent_id="agent.child",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(),
    )
    teams.set_member_state(
        team_id=team_id,
        member_id="child-1",
        state=MemberState.WAITING_APPROVAL,
        resume_token=_SECRET,
    )


def _create_experiment(store: SQLiteStore, *, experiment_id: str = "experiment-alpha") -> None:
    champion = StrategyRef(
        candidate_id="champion",
        version="1",
        artifact_kind=ArtifactKind.PROMPT,
        artifact_ref=f"artifact:{_SECRET}",
        permission_fingerprint=f"permission:{_SECRET}",
    )
    challenger = StrategyRef(
        candidate_id="challenger",
        version="1",
        artifact_kind=ArtifactKind.PROMPT,
        artifact_ref=f"challenger:{_SECRET}",
        permission_fingerprint=f"permission:{_SECRET}",
    )
    definition = ExperimentDefinition(
        experiment_id=experiment_id,
        champion=champion,
        challengers=(challenger,),
        replays=(ReplayCase("replay-1", f"dataset:{_SECRET}", "1"),),
        policy=PromotionPolicy(primary_metric="quality"),
    )
    SQLiteExperimentRepository(store).create(ExperimentSnapshot(definition=definition))


def test_snapshot_projects_bounded_restart_safe_status_without_secret_payloads(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "папка з пробілами" / "nika.db"
    store = _store(db_path)
    _create_team(store)
    _create_experiment(store)

    first = AgentLabStatusReader(store).snapshot()
    second = AgentLabStatusReader(SQLiteStore(db_path)).snapshot()

    assert first == second
    assert first.team_count == 1
    assert first.active_team_count == 1
    assert first.waiting_approval_team_count == 1
    assert first.experiment_count == 1
    assert first.running_experiment_count == 0
    assert first.teams[0].member_count == 2
    assert first.teams[0].child_count == 1
    assert first.teams[0].nonterminal_child_count == 1
    assert first.teams[0].waiting_approval_count == 1
    assert first.experiments[0].event_count == 1

    serialized = json.dumps(first.as_dict(), ensure_ascii=False, sort_keys=True)
    text = first.accessible_text()
    assert _SECRET not in serialized
    assert _SECRET not in text
    assert "tool_grants" not in serialized
    assert "resume_token" not in serialized
    assert "definition" not in serialized
    assert "artifact_ref" not in serialized
    assert "permission_fingerprint" not in serialized
    assert "Лабораторія агентів" in text
    assert "очікують підтвердження" in text


def test_state_provider_composes_without_overwriting_base_state(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    provider = AgentLabStateProvider(
        base_state=lambda: {"tasks": [{"task_id": "t-1"}]},
        status_reader=AgentLabStatusReader(store),
    )

    state = provider()

    assert state["tasks"] == [{"task_id": "t-1"}]
    assert state["agent_lab"]["team_count"] == 0
    assert state["agent_lab"]["experiment_count"] == 0


def test_state_provider_rejects_existing_agent_lab_authority(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    provider = AgentLabStateProvider(
        base_state=lambda: {"agent_lab": {"forged": True}},
        status_reader=AgentLabStatusReader(store),
    )

    with pytest.raises(RuntimeError, match="already owns"):
        provider()


def test_missing_database_fails_without_creating_it(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "nika.db"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        AgentLabStatusReader(SQLiteStore(db_path)).snapshot()

    assert not db_path.exists()
    assert not db_path.parent.exists()


@pytest.mark.parametrize("limit", [True, 0, 201])
def test_invalid_limits_fail_closed(tmp_path: Path, limit: object) -> None:
    store = SQLiteStore(tmp_path / "unused.db")

    with pytest.raises((TypeError, ValueError)):
        AgentLabStatusReader(store, limit=limit)  # type: ignore[arg-type]


def test_corrupt_quota_types_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    _create_team(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_teams SET quota_json = ? WHERE team_id = ?",
            (
                json.dumps(
                    {
                        "max_depth": True,
                        "max_children_per_parent": 3,
                        "max_total_agents": 8,
                        "max_parallel": 2,
                    }
                ),
                "team-alpha",
            ),
        )

    with pytest.raises(RuntimeError, match="quota types"):
        AgentLabStatusReader(store).snapshot()


def test_terminal_team_with_nonterminal_children_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    _create_team(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_teams SET state = 'completed' WHERE team_id = 'team-alpha'"
        )

    with pytest.raises(RuntimeError, match="terminal team"):
        AgentLabStatusReader(store).snapshot()


def test_experiment_definition_identity_corruption_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    _create_experiment(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE experiments SET definition_json = ? WHERE experiment_id = ?",
            (json.dumps({"experiment_id": "substituted"}), "experiment-alpha"),
        )

    with pytest.raises(RuntimeError, match="definition identity"):
        AgentLabStatusReader(store).snapshot()


def test_orphan_experiment_evidence_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "nika.db"
    store = _store(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO experiment_observations("
            "experiment_id, candidate_id, replay_id, metric, value, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            ("missing", "candidate", "replay", "quality", 1.0, "2026-08-26T20:00:00+00:00"),
        )

    with pytest.raises(RuntimeError, match="orphan experiment evidence"):
        AgentLabStatusReader(store).snapshot()


def test_canonical_terminal_team_allows_supervisory_root_to_remain_spawned(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "nika.db")
    teams = MultiAgentStore(store)
    teams.create_team(
        team_id="team-terminal",
        root_member_id="root",
        root_agent_id="agent.root",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(),
    )
    teams.finalize_team("team-terminal")

    snapshot = AgentLabStatusReader(store).snapshot()

    team = snapshot.teams[0]
    assert team.state == "completed"
    assert team.member_count == 1
    assert team.child_count == 0
    assert team.nonterminal_child_count == 0


def test_orphan_parent_reference_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    _create_team(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_members SET parent_id = ? "
            "WHERE team_id = ? AND member_id = ?",
            ("missing-parent", "team-alpha", "child-1"),
        )

    with pytest.raises(RuntimeError, match="orphan parent reference"):
        AgentLabStatusReader(store).snapshot()


def test_experiment_lifecycle_tail_mismatch_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    _create_experiment(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE experiments SET status = 'running' WHERE experiment_id = ?",
            ("experiment-alpha",),
        )

    with pytest.raises(RuntimeError, match="lifecycle tail"):
        AgentLabStatusReader(store).snapshot()


def test_control_character_identifier_fails_closed_before_text_output(tmp_path: Path) -> None:
    db_path = tmp_path / "nika.db"
    store = _store(db_path)
    _create_team(store)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE multi_agent_teams SET team_id = ? WHERE team_id = ?",
            ("team\nforged", "team-alpha"),
        )
        conn.execute(
            "UPDATE multi_agent_members SET team_id = ? WHERE team_id = ?",
            ("team\nforged", "team-alpha"),
        )

    with pytest.raises(RuntimeError, match="unsafe for operational output"):
        AgentLabStatusReader(store).snapshot()


def test_invalid_member_depth_lineage_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "nika.db")
    _create_team(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_members SET depth = 3 "
            "WHERE team_id = ? AND member_id = ?",
            ("team-alpha", "child-1"),
        )

    with pytest.raises(RuntimeError, match="depth lineage"):
        AgentLabStatusReader(store).snapshot()


def test_non_database_file_returns_bounded_read_error(tmp_path: Path) -> None:
    db_path = tmp_path / "nika.db"
    db_path.write_text(_SECRET, encoding="utf-8")

    with pytest.raises(RuntimeError, match="could not be read safely") as error:
        AgentLabStatusReader(SQLiteStore(db_path)).snapshot()

    assert _SECRET not in str(error.value)
