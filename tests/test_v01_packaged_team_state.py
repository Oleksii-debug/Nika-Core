from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind, MemberState, TeamQuota
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider

_SECRET_VALUES = (
    "CHECKPOINT_CANARY_SECRET",
    "PROVIDER_ERROR_CANARY_SECRET",
    "RESUME_TOKEN_CANARY_SECRET",
    "AUDIT_PAYLOAD_CANARY_SECRET",
)


def _base_state(task_id: str) -> dict[str, object]:
    return {
        "tasks": [
            {
                "task_id": task_id,
                "workspace_id": "workspace-main",
                "agent_id": "nika",
                "state": "running",
                "command": "Перевірити два контрольовані джерела.",
            }
        ],
        "agents": [],
        "workspaces": [],
        "product_project": None,
    }


def _create_team(
    store: SQLiteStore,
    *,
    task_id: str = "task-v01-71",
    include_checker: bool = True,
) -> MultiAgentStore:
    teams = MultiAgentStore(store)
    teams.create_team(
        team_id="team-v01-71",
        root_member_id="supervisor",
        root_agent_id="supervisor-agent",
        root_agent_version=1,
        root_thread_id="THREAD_SECRET_SUPERVISOR",
        root_grants=(),
        quota=TeamQuota(
            max_depth=1,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    teams.spawn_child(
        team_id="team-v01-71",
        parent_id="supervisor",
        child_id="worker",
        agent_id="worker-agent",
        agent_version=1,
        thread_id="THREAD_SECRET_WORKER",
        requested_grants=(),
        task_handoff=AgentHandoff(
            team_id="team-v01-71",
            sender_id="supervisor",
            recipient_id="worker",
            kind=HandoffKind.TASK,
            payload={
                "shared_task_id": task_id,
                "stage": "worker",
                "user_goal": "AUDIT_PAYLOAD_CANARY_SECRET",
                "assignment": "CHECKPOINT_CANARY_SECRET",
            },
        ),
    )
    teams.prepare_member_execution(
        team_id="team-v01-71",
        member_id="worker",
        resume_token="RESUME_TOKEN_CANARY_SECRET",
    )
    teams.finish_member_execution(
        team_id="team-v01-71",
        member_id="worker",
        state=MemberState.COMPLETED,
        outcome="completed",
        payload={"provider_output": "PROVIDER_ERROR_CANARY_SECRET"},
        resume_token="RESUME_TOKEN_CANARY_SECRET",
        result_handoff=AgentHandoff(
            team_id="team-v01-71",
            sender_id="worker",
            recipient_id="supervisor",
            kind=HandoffKind.RESULT,
            payload={"raw_result": "CHECKPOINT_CANARY_SECRET"},
        ),
    )

    if include_checker:
        teams.spawn_child(
            team_id="team-v01-71",
            parent_id="supervisor",
            child_id="checker",
            agent_id="checker-agent",
            agent_version=1,
            thread_id="THREAD_SECRET_CHECKER",
            requested_grants=(),
            task_handoff=AgentHandoff(
                team_id="team-v01-71",
                sender_id="supervisor",
                recipient_id="checker",
                kind=HandoffKind.TASK,
                payload={
                    "shared_task_id": task_id,
                    "stage": "checker",
                    "worker_observation": "PROVIDER_ERROR_CANARY_SECRET",
                    "assignment": "AUDIT_PAYLOAD_CANARY_SECRET",
                },
            ),
        )
    return teams


def test_real_three_member_projection_is_bounded_and_restart_stable(tmp_path) -> None:
    path = tmp_path / "nika state з пробілом.db"
    store = SQLiteStore(path)
    store.initialize()
    teams = _create_team(store)
    teams.prepare_member_execution(
        team_id="team-v01-71",
        member_id="checker",
        resume_token="RESUME_TOKEN_CANARY_SECRET",
    )
    teams.finish_member_execution(
        team_id="team-v01-71",
        member_id="checker",
        state=MemberState.COMPLETED,
        outcome="completed",
        payload={"raw_checker_result": "CHECKPOINT_CANARY_SECRET"},
        result_handoff=AgentHandoff(
            team_id="team-v01-71",
            sender_id="checker",
            recipient_id="supervisor",
            kind=HandoffKind.RESULT,
            payload={"raw_checker_handoff": "PROVIDER_ERROR_CANARY_SECRET"},
        ),
    )
    assert teams.finalize_team("team-v01-71").value == "completed"

    provider = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state("task-v01-71"),
        store=store,
    )
    first = provider()["v01_team_task"]

    assert first["available"] is True
    assert first["task"] == {
        "task_id": "task-v01-71",
        "state": "running",
        "command": "Перевірити два контрольовані джерела.",
    }
    assert first["team"] == {
        "team_id": "team-v01-71",
        "state": "completed",
        "member_count": 3,
        "expected_member_count": 3,
        "roster_complete": True,
    }
    assert [member["role"] for member in first["members"]] == [
        "supervisor",
        "worker",
        "checker",
    ]
    assert {member["state"] for member in first["members"]} == {"completed"}
    assert first["final_result"]["status"] == "completed"
    assert first["final_result"]["task_id"] == "task-v01-71"
    assert first["final_result"]["team_id"] == "team-v01-71"
    assert first["final_result"]["result_record_count"] == 2

    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    for secret in _SECRET_VALUES:
        assert secret not in serialized
    assert "THREAD_SECRET" not in serialized
    assert "thread_id" not in serialized
    assert "resume_token" not in serialized
    assert "tool_grants" not in serialized
    assert "payload_json" not in serialized
    assert '"error"' not in serialized

    restarted_store = SQLiteStore(path)
    restarted = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state("task-v01-71"),
        store=restarted_store,
    )()["v01_team_task"]
    assert restarted == first


def test_incomplete_real_roster_never_fabricates_checker(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    _create_team(store, include_checker=False)

    projected = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state("task-v01-71"),
        store=store,
    )()["v01_team_task"]

    assert projected["available"] is True
    assert projected["team"]["member_count"] == 2
    assert projected["team"]["expected_member_count"] == 3
    assert projected["team"]["roster_complete"] is False
    assert [member["role"] for member in projected["members"]] == ["supervisor", "worker"]
    assert all(member["role"] != "checker" for member in projected["members"])


def test_raw_provider_error_becomes_stable_safe_error_only(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    teams = _create_team(store)
    teams.prepare_member_execution(
        team_id="team-v01-71",
        member_id="checker",
        resume_token="RESUME_TOKEN_CANARY_SECRET",
    )
    teams.finish_member_execution(
        team_id="team-v01-71",
        member_id="checker",
        state=MemberState.FAILED,
        outcome="failed",
        payload={"raw": "CHECKPOINT_CANARY_SECRET"},
        error="PROVIDER_ERROR_CANARY_SECRET",
        result_handoff=AgentHandoff(
            team_id="team-v01-71",
            sender_id="checker",
            recipient_id="supervisor",
            kind=HandoffKind.ERROR,
            payload={"provider_exception": "AUDIT_PAYLOAD_CANARY_SECRET"},
        ),
    )
    teams.finalize_team("team-v01-71")

    projected = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state("task-v01-71"),
        store=store,
    )()["v01_team_task"]
    checker = next(member for member in projected["members"] if member["role"] == "checker")

    assert checker["safe_error"] == {
        "code": "member_failed",
        "message": "Виконання учасника завершилося помилкою.",
    }
    serialized = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    for secret in _SECRET_VALUES:
        assert secret not in serialized


def test_conflicting_shared_task_identity_fails_closed_without_raw_payload(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    _create_team(store)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT handoff_id, payload_json FROM multi_agent_handoffs "
            "WHERE team_id = ? AND recipient_id = ? AND kind = 'task'",
            ("team-v01-71", "checker"),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["shared_task_id"] = "other-task-PROVIDER_ERROR_CANARY_SECRET"
        conn.execute(
            "UPDATE multi_agent_handoffs SET payload_json = ? WHERE handoff_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["handoff_id"]),
        )

    projected = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state("task-v01-71"),
        store=store,
    )()["v01_team_task"]

    assert projected == {
        "available": False,
        "message": "Стан командного завдання недоступний.",
    }
    assert "PROVIDER_ERROR_CANARY_SECRET" not in json.dumps(projected, ensure_ascii=False)


def test_non_v01_team_is_not_misrepresented_as_packaged_three_agent_task(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    teams = MultiAgentStore(store)
    teams.create_team(
        team_id="generic-team",
        root_member_id="root",
        root_agent_id="root-agent",
        root_agent_version=1,
        root_thread_id="generic-thread",
        root_grants=(),
        quota=TeamQuota(),
    )
    teams.spawn_child(
        team_id="generic-team",
        parent_id="root",
        child_id="child",
        agent_id="child-agent",
        agent_version=1,
        thread_id="generic-child-thread",
        requested_grants=(),
        task_handoff=AgentHandoff(
            team_id="generic-team",
            sender_id="root",
            recipient_id="child",
            kind=HandoffKind.TASK,
            payload={"assignment": "ordinary generic task"},
        ),
    )

    projected = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state("task-v01-71"),
        store=store,
    )()["v01_team_task"]
    assert projected is None


def test_projection_key_collision_is_rejected_before_overwrite(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    provider = V01PackagedTeamStateProvider(
        base_state=lambda: {"v01_team_task": {"unexpected": True}},
        store=store,
    )

    with pytest.raises(ValueError, match="projection key collision"):
        provider()
