from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskPayloadCorruptionError, TaskQueue
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime


def _configured_runtime(tmp_path: Path) -> tuple[SQLiteStore, V01PackagedThreeAgentRuntime]:
    source_a = tmp_path / "source-a.txt"
    source_b = tmp_path / "source-b.txt"
    source_a.write_text("same controlled evidence", encoding="utf-8")
    source_b.write_text("same controlled evidence", encoding="utf-8")
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    config = AppConfig(
        database_path=tmp_path / "nika.db",
        v01_source_root=tmp_path,
        v01_source_a=source_a,
        v01_source_b=source_b,
    )
    return store, V01PackagedThreeAgentRuntime(store=store, config=config)


def _result_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM multi_agent_results").fetchone()
    assert row is not None
    return int(row["count"])


def _created_task(store: SQLiteStore, command: str) -> str:
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    ).task_id


def test_packaged_runtime_executes_canonical_three_agent_team(tmp_path: Path) -> None:
    store, runtime = _configured_runtime(tmp_path)
    task_id = _created_task(store, "Compare the two declared local sources.")
    request = RuntimeRequest(
        task_id=task_id,
        thread_id=f"desktop-{task_id}",
        payload={"command": "Compare the two declared local sources."},
    )

    result = asyncio.run(runtime.run(request))

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["task_id"] == task_id
    team_id = str(result.output["team_id"])
    with store.connection() as conn:
        members = conn.execute(
            "SELECT member_id, state FROM multi_agent_members "
            "WHERE team_id = ? ORDER BY member_id",
            (team_id,),
        ).fetchall()
    assert [str(row["member_id"]) for row in members] == [
        "checker",
        "worker-a",
        "worker-b",
    ]
    assert all(str(row["state"]) == "completed" for row in members)
    assert _result_count(store) == 3


def test_packaged_runtime_resume_replays_terminal_team_without_member_rerun(
    tmp_path: Path,
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    task_id = _created_task(store, "Compare the two declared local sources.")
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=thread_id,
                payload={"command": "Compare the two declared local sources."},
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    result_count = _result_count(store)
    token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)

    resumed = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=thread_id,
                resume_token=token,
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )

    assert resumed.outcome is RuntimeOutcome.COMPLETED
    assert resumed.output["team_id"] == first.output["team_id"]
    assert _result_count(store) == result_count


def test_packaged_runtime_missing_source_config_fails_closed_without_team(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=AppConfig(database_path=tmp_path / "nika.db"),
    )
    task_id = _created_task(store, "Run the representative team.")

    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": "Run the representative team."},
            )
        )
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error == "V0.1 packaged three-agent execution failed closed."
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM multi_agent_teams").fetchone()
    assert row is not None
    assert int(row["count"]) == 0


def test_packaged_runtime_rejects_source_outside_declared_root_without_team(
    tmp_path: Path,
) -> None:
    declared_root = tmp_path / "declared-root"
    declared_root.mkdir()
    source_a = tmp_path / "outside-a.txt"
    source_b = tmp_path / "outside-b.txt"
    source_a.write_text("outside controlled evidence A", encoding="utf-8")
    source_b.write_text("outside controlled evidence B", encoding="utf-8")
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=AppConfig(
            database_path=tmp_path / "nika.db",
            v01_source_root=declared_root,
            v01_source_a=source_a,
            v01_source_b=source_b,
        ),
    )

    task_id = _created_task(store, "Run the representative team.")
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": "Run the representative team."},
            )
        )
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error == "V0.1 packaged three-agent execution failed closed."
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM multi_agent_teams").fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.parametrize(
    "persisted",
    [
        pytest.param("[]", id="array"),
        pytest.param('{"v01_model_selection":"a","v01_model_selection":"b"}',
                     id="ambiguous-model-selection"),
        pytest.param('{"command":"Порівняй","score":NaN}', id="nonfinite"),
        pytest.param(sqlite3.Binary(b'{"command":"valid"}'), id="blob"),
    ],
)
def test_packaged_runtime_uses_canonical_task_payload_for_model_and_resume(
    tmp_path: Path, persisted: object
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Порівняй"},
    )
    assert runtime._task_has_model_selection(task.task_id) is False
    assert runtime._stored_outer_command(task.task_id) == "Порівняй"
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (persisted, task.task_id),
        )
    with pytest.raises(TaskPayloadCorruptionError, match="пошкоджені"):
        runtime._task_has_model_selection(task.task_id)
    assert runtime._stored_outer_command(task.task_id) == ""


def test_unknown_packaged_task_cannot_create_orphan_source_binding(
    tmp_path: Path,
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    missing_id = "never-queued"
    missing_thread = f"desktop-{missing_id}"
    probe = asyncio.run(
        runtime.probe_resume(
            task_id=missing_id,
            thread_id=missing_thread,
            resume_token=runtime.initial_resume_token(
                task_id=missing_id, thread_id=missing_thread
            ),
        )
    )
    assert probe.status is RuntimeResumeProbeStatus.INVALID
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=missing_id,
                thread_id=missing_thread,
                payload={"command": "Compare the two declared local sources."},
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (missing_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_teams").fetchone()[0] == 0


@pytest.mark.parametrize(
    "persisted",
    [
        pytest.param("[]", id="non-object"),
        pytest.param('{"command":"a","command":"b"}', id="duplicate-command"),
        pytest.param(sqlite3.Binary(b'{"command":"test"}'), id="sqlite-blob"),
    ],
)
def test_packaged_resume_rejects_corrupt_task_even_with_cached_checker_goal(
    tmp_path: Path, persisted: object
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=thread_id,
                payload={"command": command},
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    assert runtime._multi_store.task_payload(runtime._team_id(task_id), "checker")[
        "user_goal"
    ] == command
    prior_results = _result_count(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (persisted, task_id),
        )

    assert runtime._stored_outer_command(task_id) == ""
    token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    outer_probe = asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=token
        )
    )
    assert outer_probe.status is RuntimeResumeProbeStatus.INVALID
    assert outer_probe.checkpoint_id is None

    team_id = runtime._team_id(task_id)
    member_thread = f"v01:{team_id}:worker-a"
    member_task_id = f"team:{team_id}:worker-a"
    member_token = runtime.initial_resume_token(
        task_id=member_task_id, thread_id=member_thread
    )
    member_probe = asyncio.run(
        runtime.probe_resume(
            task_id=member_task_id,
            thread_id=member_thread,
            resume_token=member_token,
        )
    )
    assert member_probe.status is RuntimeResumeProbeStatus.INVALID
    member_result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=member_task_id,
                thread_id=member_thread,
                resume_token=member_token,
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    assert member_result.outcome is RuntimeOutcome.FAILED

    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=thread_id,
                resume_token=runtime.initial_resume_token(
                    task_id=task_id, thread_id=thread_id
                ),
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    assert _result_count(store) == prior_results


def test_packaged_cached_legacy_goal_survives_missing_task_row(tmp_path: Path) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=thread_id,
                payload={"command": command},
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    with store.connection() as conn:
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    assert runtime._multi_store.task_payload(runtime._team_id(task_id), "checker")[
        "user_goal"
    ] == command
    assert runtime._stored_outer_command(task_id) == command
    outer_token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    assert asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=outer_token
        )
    ).status is RuntimeResumeProbeStatus.READY
    team_id = runtime._team_id(task_id)
    member_task_id = f"team:{team_id}:worker-a"
    member_thread = f"v01:{team_id}:worker-a"
    member_token = runtime.initial_resume_token(
        task_id=member_task_id, thread_id=member_thread
    )
    assert asyncio.run(
        runtime.probe_resume(
            task_id=member_task_id,
            thread_id=member_thread,
            resume_token=member_token,
        )
    ).status is RuntimeResumeProbeStatus.READY


def test_member_resume_never_rebinds_another_member_task_id(tmp_path: Path) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": command},
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    previous_results = _result_count(store)
    team_id = runtime._team_id(task_id)
    member_thread = f"v01:{team_id}:worker-a"
    correct_task_id = f"team:{team_id}:worker-a"
    correct_token = runtime.initial_resume_token(
        task_id=correct_task_id, thread_id=member_thread
    )
    assert asyncio.run(
        runtime.probe_resume(
            task_id=correct_task_id,
            thread_id=member_thread,
            resume_token=correct_token,
        )
    ).status is RuntimeResumeProbeStatus.READY
    forged_task_id = f"team:{team_id}:worker-b"
    forged_token = runtime.initial_resume_token(
        task_id=forged_task_id, thread_id=member_thread
    )
    assert asyncio.run(
        runtime.probe_resume(
            task_id=forged_task_id,
            thread_id=member_thread,
            resume_token=forged_token,
        )
    ).status is RuntimeResumeProbeStatus.INVALID
    forged_resume = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=forged_task_id,
                thread_id=member_thread,
                resume_token=forged_token,
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    assert forged_resume.outcome is RuntimeOutcome.FAILED
    forged_run = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=forged_task_id,
                thread_id=member_thread,
                payload={"command": command},
            )
        )
    )
    assert forged_run.outcome is RuntimeOutcome.FAILED
    assert _result_count(store) == previous_results


@pytest.mark.parametrize(
    "persisted",
    [
        pytest.param("[]", id="non-object"),
        pytest.param(b'{"shared_task_id":"untrusted"}', id="sqlite-blob"),
    ],
)
def test_corrupt_checker_handoff_cannot_authorize_outer_resume(
    tmp_path: Path, persisted: object
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id, thread_id=thread_id, payload={"command": command}
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    previous_results = _result_count(store)
    with store.connection() as conn:
        changed = conn.execute(
            "UPDATE multi_agent_handoffs SET payload_json = ? "
            "WHERE team_id = ? AND recipient_id = 'checker' AND kind = 'task'",
            (persisted, runtime._team_id(task_id)),
        )
        assert changed.rowcount == 1

    assert runtime._stored_outer_command(task_id) == ""
    token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    probe = asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=token
        )
    )
    assert probe.status is RuntimeResumeProbeStatus.INVALID
    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=thread_id,
                resume_token=token,
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    assert _result_count(store) == previous_results


@pytest.mark.parametrize(
    "supplied",
    [
        pytest.param(True, id="boolean"),
        pytest.param(42, id="number"),
        pytest.param({"goal": "other"}, id="object"),
        pytest.param("Different instruction.", id="mismatched-text"),
    ],
)
def test_initial_outer_run_rejects_nontext_or_unbound_command(
    tmp_path: Path, supplied: object
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    task_id = _created_task(store, "Compare the two declared local sources.")
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": supplied},
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_teams").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("carrier", "replacement"),
    [
        pytest.param("task", True, id="task-boolean"),
        pytest.param("task", None, id="task-null"),
        pytest.param("task", "", id="task-empty"),
        pytest.param("task", {"goal": "other"}, id="task-object"),
        pytest.param("task", "Different instruction.", id="task-mismatch"),
        pytest.param("checker", True, id="checker-boolean"),
        pytest.param("checker", {"goal": "other"}, id="checker-object"),
        pytest.param("checker", None, id="checker-null"),
        pytest.param("checker", "Different instruction.", id="checker-mismatch"),
    ],
)
def test_resume_rejects_malformed_or_mismatched_persisted_command(
    tmp_path: Path, carrier: str, replacement: object
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=thread_id,
                payload={"command": command},
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    team_id = runtime._team_id(task_id)
    prior_results = _result_count(store)
    with store.connection() as conn:
        if carrier == "task":
            cursor = conn.execute(
                "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
                (json.dumps({"command": replacement}), task_id),
            )
        else:
            row = conn.execute(
                "SELECT handoff_id, payload_json FROM multi_agent_handoffs "
                "WHERE team_id = ? AND recipient_id = 'checker' AND kind = 'task'",
                (team_id,),
            ).fetchone()
            assert row is not None
            handoff = json.loads(row["payload_json"])
            handoff["user_goal"] = replacement
            cursor = conn.execute(
                "UPDATE multi_agent_handoffs SET payload_json = ? WHERE handoff_id = ?",
                (json.dumps(handoff, ensure_ascii=False), row["handoff_id"]),
            )
        assert cursor.rowcount == 1

    assert runtime._stored_outer_command(task_id) == ""
    outer_token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    assert asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=outer_token
        )
    ).status is RuntimeResumeProbeStatus.INVALID

    member_task_id = f"team:{team_id}:worker-a"
    member_thread = f"v01:{team_id}:worker-a"
    member_token = runtime.initial_resume_token(
        task_id=member_task_id, thread_id=member_thread
    )
    assert asyncio.run(
        runtime.probe_resume(
            task_id=member_task_id,
            thread_id=member_thread,
            resume_token=member_token,
        )
    ).status is RuntimeResumeProbeStatus.INVALID
    assert asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=thread_id,
                resume_token=outer_token,
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    ).outcome is RuntimeOutcome.FAILED
    assert _result_count(store) == prior_results
    assert asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=thread_id,
                payload={"command": command},
            )
        )
    ).outcome is RuntimeOutcome.FAILED
    assert _result_count(store) == prior_results

    reopened = V01PackagedThreeAgentRuntime(
        store=SQLiteStore(store.path), config=AppConfig(database_path=store.path)
    )
    assert reopened._stored_outer_command(task_id) == ""


def test_unicode_and_whitespace_command_remains_restart_safe(tmp_path: Path) -> None:
    store, runtime = _configured_runtime(tmp_path)
    task_id = _created_task(store, "  Порівняй два локальні джерела. \t")
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=thread_id,
                payload={"command": "Порівняй два локальні джерела."},
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    assert runtime._stored_outer_command(task_id) == "Порівняй два локальні джерела."
    token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    assert asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=token
        )
    ).status is RuntimeResumeProbeStatus.READY
    assert asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=thread_id,
                resume_token=token,
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    ).outcome is RuntimeOutcome.COMPLETED


@pytest.mark.parametrize("fallback", ("blank", "missing"))
def test_legacy_empty_checker_goal_uses_valid_durable_task_command(
    tmp_path: Path, fallback: str
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id, thread_id=thread_id, payload={"command": command}
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    with store.connection() as conn:
        row = conn.execute(
            "SELECT handoff_id, payload_json FROM multi_agent_handoffs "
            "WHERE team_id = ? AND recipient_id = 'checker' AND kind = 'task'",
            (runtime._team_id(task_id),),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        if fallback == "blank":
            payload["user_goal"] = ""
        else:
            payload.pop("user_goal", None)
        conn.execute(
            "UPDATE multi_agent_handoffs SET payload_json = ? WHERE handoff_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["handoff_id"]),
        )
    assert runtime._stored_outer_command(task_id) == command
    token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    assert asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=token
        )
    ).status is RuntimeResumeProbeStatus.READY


@pytest.mark.parametrize(
    "stored_command",
    [
        pytest.param(True, id="nontext-stored-command"),
        pytest.param("Changed after queueing.", id="stale-request-command"),
    ],
)
def test_first_run_rejects_corrupt_or_rebound_durable_command_before_effects(
    tmp_path: Path, stored_command: object
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (json.dumps({"command": stored_command}), task_id),
        )
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": command},
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_teams").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0

def test_initial_run_rejects_missing_task_even_with_legacy_checker_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, runtime = _configured_runtime(tmp_path)
    command = "Compare the two declared local sources."
    task_id = _created_task(store, command)
    thread_id = f"desktop-{task_id}"
    first = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id, thread_id=thread_id, payload={"command": command}
            )
        )
    )
    assert first.outcome is RuntimeOutcome.COMPLETED
    previous_results = _result_count(store)
    real_get = TaskQueue.get

    def missing_get(queue: TaskQueue, requested_id: str):
        if requested_id == task_id:
            raise KeyError("simulated missing queued task")
        return real_get(queue, requested_id)

    monkeypatch.setattr(TaskQueue, "get", missing_get)
    # Preserve legacy resume's authentic cached goal, but never use it to
    # authorize a fresh run when the canonical queue entry is absent.
    assert runtime._stored_outer_command(task_id) == command
    token = runtime.initial_resume_token(task_id=task_id, thread_id=thread_id)
    assert asyncio.run(
        runtime.probe_resume(
            task_id=task_id, thread_id=thread_id, resume_token=token
        )
    ).status is RuntimeResumeProbeStatus.READY
    second = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id, thread_id=thread_id, payload={"command": command}
            )
        )
    )
    assert second.outcome is RuntimeOutcome.FAILED
    assert _result_count(store) == previous_results
