from __future__ import annotations

import json
from time import monotonic, sleep

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.multi_agent.checker import V01CheckerAgent
from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind
from nika_core.multi_agent.research_results import SourceInspectionAssignment
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider
from scripts.nika_windows import build_windows_bridge

_RAW_SOURCE_CANARY = "FINAL_RESULT_RAW_SOURCE_CANARY"


def _base_state(queue: TaskQueue, task_id: str) -> dict[str, object]:
    task = queue.get(task_id)
    return {
        "tasks": [
            {
                "task_id": task.task_id,
                "workspace_id": task.workspace_id,
                "agent_id": task.agent_id,
                "state": task.state.value,
                "command": str(task.payload.get("command", "")),
            }
        ],
        "agents": [],
        "workspaces": [],
        "product_project": None,
    }


def _complete_packaged_task(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_b_text: str,
):
    source_root = tmp_path / "Джерела результату"
    source_root.mkdir()
    source_a = source_root / "А.txt"
    source_b = source_root / "Б.txt"
    source_a.write_text(_RAW_SOURCE_CANARY, encoding="utf-8")
    source_b.write_text(source_b_text, encoding="utf-8")
    config = AppConfig(database_path=tmp_path / "ніка final result.db")

    pending = []
    original_start = DesktopBackend._schedule_start
    monkeypatch.setattr(
        DesktopBackend,
        "_schedule_start",
        lambda self, task, command: pending.append((self, task, command)),
    )
    bridge, _products = build_windows_bridge(config)
    configured = bridge.dispatch(
        {
            "request_id": "safe-final-sources",
            "action_id": "team.sources.configure",
            "payload": {
                "root": str(source_root),
                "source_a": source_a.name,
                "source_b": source_b.name,
                "revision": 0,
            },
        }
    )
    assert configured["status"] == "completed"
    created = bridge.dispatch(
        {
            "request_id": "safe-final-task",
            "action_id": "task.create",
            "payload": {"command": "Порівняй два контрольовані джерела."},
        }
    )
    assert created["status"] == "accepted"
    backend, task_id, command = pending.pop()
    original_start(backend, task_id, command)

    store = SQLiteStore(config.database_path)
    queue = TaskQueue(store)
    deadline = monotonic() + 20
    while queue.get(task_id).state in {TaskState.READY, TaskState.RUNNING}:
        assert monotonic() < deadline, "Packaged three-agent task did not finish in 20 seconds"
        sleep(0.01)
    backend.close()
    assert queue.get(task_id).state is TaskState.COMPLETED

    provider = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state(queue, task_id),
        store=store,
    )
    projection = provider()["v01_team_task"]
    assert projection["available"] is True
    return store, queue, task_id, projection, provider


@pytest.mark.parametrize(
    ("source_b_text", "expected_status", "agreement_count", "difference_count"),
    [
        (_RAW_SOURCE_CANARY, "agree", 1, 0),
        ("DIFFERENT_RAW_SOURCE_CANARY", "disagree", 0, 1),
    ],
)
def test_completed_canonical_team_projects_only_bounded_validated_checker_verdict(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    source_b_text: str,
    expected_status: str,
    agreement_count: int,
    difference_count: int,
) -> None:
    store, queue, task_id, projection, _provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=source_b_text,
    )

    comparison = projection["final_result"]["comparison"]
    assert comparison == {
        "status": expected_status,
        "validated": True,
        "source_states": ["valid", "valid"],
        "agreement_count": agreement_count,
        "difference_count": difference_count,
    }

    serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    assert _RAW_SOURCE_CANARY not in serialized
    assert "DIFFERENT_RAW_SOURCE_CANARY" not in serialized
    assert "result_set" not in serialized
    assert "snippet" not in serialized
    assert "locator" not in serialized
    assert "assignment_id" not in serialized

    restarted = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state(queue, task_id),
        store=SQLiteStore(store.path),
    )()["v01_team_task"]
    assert restarted == projection


def test_corrupt_persisted_checker_result_fails_closed_without_raw_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue, task_id, projection, provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=_RAW_SOURCE_CANARY,
    )
    team_id = projection["team"]["team_id"]
    with store.connection() as conn:
        row = conn.execute(
            "SELECT result_id, payload_json FROM multi_agent_results "
            "WHERE team_id = ? AND member_id = ?",
            (team_id, "checker"),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["checker_summary"]["task_id"] = "CORRUPT_CHECKER_RAW_CANARY"
        conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? WHERE result_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["result_id"]),
        )

    corrupted = provider()["v01_team_task"]
    comparison = corrupted["final_result"]["comparison"]
    assert comparison == {
        "status": "evidence_invalid",
        "validated": False,
        "source_states": [],
        "agreement_count": 0,
        "difference_count": 0,
    }
    serialized = json.dumps(corrupted, ensure_ascii=False, sort_keys=True)
    assert "CORRUPT_CHECKER_RAW_CANARY" not in serialized
    assert _RAW_SOURCE_CANARY not in serialized
    assert queue.get(task_id).state is TaskState.COMPLETED


def _canonical_checker_summary(store: SQLiteStore, team_id: str) -> dict[str, object]:
    with store.connection() as conn:
        team = conn.execute(
            "SELECT root_member_id FROM multi_agent_teams WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        assert team is not None
        checker_id = str(team["root_member_id"])
        task_row = conn.execute(
            "SELECT payload_json FROM multi_agent_handoffs "
            "WHERE team_id = ? AND recipient_id = ? AND kind = 'task' "
            "ORDER BY created_at, handoff_id",
            (team_id, checker_id),
        ).fetchone()
        assert task_row is not None
        checker_task = json.loads(task_row["payload_json"])
        assignments = tuple(
            SourceInspectionAssignment.from_payload(item)
            for item in checker_task["source_assignments"]
        )
        handoff_rows = conn.execute(
            "SELECT handoff_id, team_id, sender_id, recipient_id, kind, correlation_id, "
            "payload_json FROM multi_agent_handoffs "
            "WHERE team_id = ? AND recipient_id = ? AND kind IN ('result', 'error') "
            "ORDER BY created_at, handoff_id",
            (team_id, checker_id),
        ).fetchall()
        handoffs = tuple(
            AgentHandoff(
                handoff_id=str(row["handoff_id"]),
                team_id=str(row["team_id"]),
                sender_id=str(row["sender_id"]),
                recipient_id=str(row["recipient_id"]),
                kind=HandoffKind(str(row["kind"])),
                correlation_id=str(row["correlation_id"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in handoff_rows
        )
        return V01CheckerAgent().compare(
            team_id=team_id,
            task_id=str(checker_task["shared_task_id"]),
            checker_id=checker_id,
            assignments=assignments,
            handoffs=handoffs,
        ).to_payload()


def _persist_checker_summary(
    store: SQLiteStore,
    team_id: str,
    summary: dict[str, object],
) -> None:
    with store.connection() as conn:
        team = conn.execute(
            "SELECT root_member_id FROM multi_agent_teams WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        assert team is not None
        checker_id = str(team["root_member_id"])
        cursor = conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? "
            "WHERE team_id = ? AND member_id = ?",
            (
                json.dumps(
                    {"checker_summary": summary},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                team_id,
                checker_id,
            ),
        )
        assert cursor.rowcount == 1


def test_duplicate_worker_handoff_stays_evidence_invalid_and_unvalidated(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _queue, _task_id, projection, provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=_RAW_SOURCE_CANARY,
    )
    team_id = projection["team"]["team_id"]
    with store.connection() as conn:
        source = conn.execute(
            "SELECT handoff_id, team_id, sender_id, recipient_id, kind, correlation_id, "
            "payload_json, created_at FROM multi_agent_handoffs "
            "WHERE team_id = ? AND sender_id = 'worker-a' AND kind = 'result'",
            (team_id,),
        ).fetchone()
        assert source is not None
        conn.execute(
            "INSERT INTO multi_agent_handoffs("
            "handoff_id, team_id, sender_id, recipient_id, kind, correlation_id, "
            "payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "duplicate-result-handoff",
                source["team_id"],
                source["sender_id"],
                source["recipient_id"],
                source["kind"],
                source["correlation_id"],
                source["payload_json"],
                source["created_at"],
            ),
        )

    summary = _canonical_checker_summary(store, team_id)
    assert summary["status"] == "evidence_invalid"
    _persist_checker_summary(store, team_id, summary)

    comparison = provider()["v01_team_task"]["final_result"]["comparison"]
    assert comparison["status"] == "evidence_invalid"
    assert comparison["validated"] is False
    assert "evidence_invalid" in comparison["source_states"]


def test_worker_error_summary_is_reproducible_but_not_evidence_validated(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _queue, _task_id, projection, provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=_RAW_SOURCE_CANARY,
    )
    team_id = projection["team"]["team_id"]
    with store.connection() as conn:
        cursor = conn.execute(
            "UPDATE multi_agent_handoffs SET kind = 'error', payload_json = ? "
            "WHERE team_id = ? AND sender_id = 'worker-a' AND kind = 'result'",
            (
                json.dumps({"error": "RuntimeError"}, sort_keys=True, separators=(",", ":")),
                team_id,
            ),
        )
        assert cursor.rowcount == 1

    summary = _canonical_checker_summary(store, team_id)
    assert summary["status"] == "worker_error"
    _persist_checker_summary(store, team_id, summary)

    comparison = provider()["v01_team_task"]["final_result"]["comparison"]
    assert comparison["status"] == "worker_error"
    assert comparison["validated"] is False
    assert "worker_error" in comparison["source_states"]


def test_checker_assignment_must_match_durable_worker_task_assignment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _queue, _task_id, projection, provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=_RAW_SOURCE_CANARY,
    )
    team_id = projection["team"]["team_id"]
    with store.connection() as conn:
        row = conn.execute(
            "SELECT handoff_id, payload_json FROM multi_agent_handoffs "
            "WHERE team_id = ? AND recipient_id = 'worker-a' AND kind = 'task'",
            (team_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["source_assignment"]["max_items"] = 2
        conn.execute(
            "UPDATE multi_agent_handoffs SET payload_json = ? WHERE handoff_id = ?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                row["handoff_id"],
            ),
        )

    comparison = provider()["v01_team_task"]["final_result"]["comparison"]
    assert comparison == {
        "status": "evidence_invalid",
        "validated": False,
        "source_states": [],
        "agreement_count": 0,
        "difference_count": 0,
    }


def test_checker_assignments_must_name_exact_canonical_worker_roster(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _queue, _task_id, projection, provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=_RAW_SOURCE_CANARY,
    )
    team_id = projection["team"]["team_id"]
    with store.connection() as conn:
        row = conn.execute(
            "SELECT handoff_id, payload_json FROM multi_agent_handoffs "
            "WHERE team_id = ? AND recipient_id = 'checker' AND kind = 'task'",
            (team_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["source_assignments"][0]["member_id"] = "foreign-worker"
        conn.execute(
            "UPDATE multi_agent_handoffs SET payload_json = ? WHERE handoff_id = ?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                row["handoff_id"],
            ),
        )

    comparison = provider()["v01_team_task"]["final_result"]["comparison"]
    assert comparison["status"] == "evidence_invalid"
    assert comparison["validated"] is False


def test_checker_result_requires_exact_wrapper_and_does_not_leak_extra_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _queue, _task_id, projection, provider = _complete_packaged_task(
        tmp_path,
        monkeypatch,
        source_b_text=_RAW_SOURCE_CANARY,
    )
    team_id = projection["team"]["team_id"]
    raw_canary = "EXTRA_CHECKER_RESULT_RAW_CANARY"
    with store.connection() as conn:
        row = conn.execute(
            "SELECT result_id, payload_json FROM multi_agent_results "
            "WHERE team_id = ? AND member_id = 'checker'",
            (team_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["foreign_raw"] = raw_canary
        conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? WHERE result_id = ?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                row["result_id"],
            ),
        )

    corrupted = provider()["v01_team_task"]
    comparison = corrupted["final_result"]["comparison"]
    assert comparison["status"] == "evidence_invalid"
    assert comparison["validated"] is False
    assert raw_canary not in json.dumps(corrupted, ensure_ascii=False, sort_keys=True)
