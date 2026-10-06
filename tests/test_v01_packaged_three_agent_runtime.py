from __future__ import annotations

import asyncio
from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
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


def test_packaged_runtime_executes_canonical_three_agent_team(tmp_path: Path) -> None:
    store, runtime = _configured_runtime(tmp_path)
    task_id = "packaged-task-001"
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
    task_id = "packaged-task-restart"
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
    task_id = "packaged-task-unconfigured"

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

    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id="packaged-task-outside-root",
                thread_id="desktop-packaged-task-outside-root",
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
