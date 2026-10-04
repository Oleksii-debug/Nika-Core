from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskPayloadCorruptionError
from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind, TeamQuota
from nika_core.multi_agent.store import MultiAgentStore

_TEAM_ID = "durable-team"
_ROOT = "checker"
_CHILD = "worker"


def _stored_team(tmp_path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "Команда з пробілами.db")
    sqlite.initialize()
    teams = MultiAgentStore(sqlite)
    teams.create_team(
        team_id=_TEAM_ID,
        root_member_id=_ROOT,
        root_agent_id="agent.checker",
        root_agent_version=1,
        root_thread_id="thread-checker",
        root_grants=(),
        quota=TeamQuota(),
        root_task_handoff=AgentHandoff(
            team_id=_TEAM_ID,
            sender_id=_ROOT,
            recipient_id=_ROOT,
            kind=HandoffKind.TASK,
            payload={"stage": "checker", "shared_task_id": "task-1"},
        ),
    )
    teams.spawn_child(
        team_id=_TEAM_ID,
        parent_id=_ROOT,
        child_id=_CHILD,
        agent_id="agent.worker",
        agent_version=1,
        thread_id="thread-worker",
        requested_grants=(),
        task_handoff=AgentHandoff(
            team_id=_TEAM_ID,
            sender_id=_ROOT,
            recipient_id=_CHILD,
            kind=HandoffKind.TASK,
            payload={"stage": "source_worker", "shared_task_id": "task-1"},
        ),
    )
    teams.record_handoff(
        AgentHandoff(
            team_id=_TEAM_ID,
            sender_id=_CHILD,
            recipient_id=_ROOT,
            kind=HandoffKind.RESULT,
            payload={"verdict": "перевірено"},
        )
    )
    teams.record_result(
        team_id=_TEAM_ID,
        member_id=_CHILD,
        outcome="completed",
        payload={"status": "перевірено"},
    )
    return sqlite, teams


def test_valid_text_handoffs_and_results_reopen_without_payload_changes(tmp_path) -> None:
    sqlite, teams = _stored_team(tmp_path)
    assert teams.task_payload(_TEAM_ID, _ROOT) == {
        "stage": "checker",
        "shared_task_id": "task-1",
    }
    assert teams.inbound_result_handoffs(_TEAM_ID, _ROOT)[0].payload == {
        "verdict": "перевірено"
    }
    assert teams.member_result(_TEAM_ID, _CHILD).payload == {
        "status": "перевірено"
    }

    reopened = MultiAgentStore(SQLiteStore(sqlite.path))
    assert reopened.task_payload(_TEAM_ID, _ROOT) == teams.task_payload(_TEAM_ID, _ROOT)
    assert reopened.inbound_result_handoffs(_TEAM_ID, _ROOT)[0].payload == {
        "verdict": "перевірено"
    }
    assert reopened.member_result(_TEAM_ID, _CHILD).payload == {
        "status": "перевірено"
    }


@pytest.mark.parametrize("carrier", ("task", "result_handoff", "member_result"))
@pytest.mark.parametrize(
    "corruption",
    (
        pytest.param('{"status":1,"status":1}', id="duplicate-key"),
        pytest.param('{"nested":{"status":1,"status":1}}', id="nested-duplicate"),
        pytest.param('{"status":NaN}', id="nonfinite"),
        pytest.param('{"status":1e999}', id="overflow"),
        pytest.param(b'{"status":"valid"}', id="sqlite-blob"),
        pytest.param("[]", id="non-object"),
        pytest.param('{"status":', id="truncated"),
    ),
)
def test_corrupt_persisted_payload_never_reconstructs_as_valid_object(
    tmp_path, carrier: str, corruption: object
) -> None:
    sqlite, _teams = _stored_team(tmp_path)
    with sqlite.connection() as conn:
        if carrier == "task":
            changed = conn.execute(
                "UPDATE multi_agent_handoffs SET payload_json = ? "
                "WHERE team_id = ? AND recipient_id = ? AND kind = 'task'",
                (corruption, _TEAM_ID, _ROOT),
            )
        elif carrier == "result_handoff":
            changed = conn.execute(
                "UPDATE multi_agent_handoffs SET payload_json = ? "
                "WHERE team_id = ? AND recipient_id = ? AND kind = 'result'",
                (corruption, _TEAM_ID, _ROOT),
            )
        else:
            changed = conn.execute(
                "UPDATE multi_agent_results SET payload_json = ? "
                "WHERE team_id = ? AND member_id = ?",
                (corruption, _TEAM_ID, _CHILD),
            )
        assert changed.rowcount == 1

    reopened = MultiAgentStore(SQLiteStore(sqlite.path))
    with pytest.raises(TaskPayloadCorruptionError, match="пошкоджені"):
        if carrier == "task":
            reopened.task_payload(_TEAM_ID, _ROOT)
        elif carrier == "result_handoff":
            reopened.inbound_result_handoffs(_TEAM_ID, _ROOT)
        else:
            reopened.member_result(_TEAM_ID, _CHILD)
