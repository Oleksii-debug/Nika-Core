from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)
from nika_core.runtime.langgraph_runtime import LangGraphRuntime, open_langgraph_sqlite


class _RuntimeState(TypedDict, total=False):
    prepared: bool
    approved: bool
    result: str


def _append_line(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(value + "\n")


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _build_failure_then_resume_graph(*, side_effect_file: Path, failure_marker: Path, checkpointer):
    def prepare(state: _RuntimeState) -> _RuntimeState:
        _append_line(side_effect_file, "prepared")
        return {"prepared": True}

    def flaky_finish(state: _RuntimeState) -> _RuntimeState:
        if not failure_marker.exists():
            failure_marker.write_text("failed-once", encoding="utf-8")
            raise RuntimeError("intentional transient failure")
        return {"result": "completed-after-restart"}

    builder = StateGraph(_RuntimeState)
    builder.add_node("prepare", prepare)
    builder.add_node("finish", flaky_finish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


def _build_approval_graph(*, side_effect_file: Path, checkpointer):
    def prepare(state: _RuntimeState) -> _RuntimeState:
        _append_line(side_effect_file, "prepared")
        return {"prepared": True}

    def approval(state: _RuntimeState) -> _RuntimeState:
        approved = interrupt({"question": "approve?"})
        return {"approved": bool(approved), "result": "approved" if approved else "rejected"}

    builder = StateGraph(_RuntimeState)
    builder.add_node("prepare", prepare)
    builder.add_node("approval", approval)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=checkpointer)


def test_completed_step_is_not_repeated_after_process_object_recreation(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "runtime.sqlite"
        side_effect = tmp_path / "side-effects.log"
        failure_marker = tmp_path / "failure.marker"
        thread_id = "restart-proof"

        async with open_langgraph_sqlite(database) as first_handle:
            first_graph = _build_failure_then_resume_graph(
                side_effect_file=side_effect,
                failure_marker=failure_marker,
                checkpointer=first_handle.checkpointer,
            )
            first_runtime = LangGraphRuntime(first_graph)
            failed = await first_runtime.run(
                RuntimeRequest("task-restart", thread_id, {"prepared": False}, max_steps=20)
            )
            assert failed.outcome == RuntimeOutcome.FAILED
            assert "intentional transient failure" in (failed.error or "")
            assert _line_count(side_effect) == 1

        # The connection, compiled graph and runtime above are all gone. A fresh set of objects
        # must resume the persisted thread without re-running the completed prepare node.
        async with open_langgraph_sqlite(database) as second_handle:
            second_graph = _build_failure_then_resume_graph(
                side_effect_file=side_effect,
                failure_marker=failure_marker,
                checkpointer=second_handle.checkpointer,
            )
            second_runtime = LangGraphRuntime(second_graph)
            resumed = await second_runtime.resume(
                RuntimeResumeRequest(
                    task_id="task-restart",
                    thread_id=thread_id,
                    resume_token=thread_id,
                    mode=RuntimeResumeMode.CONTINUE,
                    max_steps=20,
                )
            )
            assert resumed.outcome == RuntimeOutcome.COMPLETED
            assert resumed.output["result"] == "completed-after-restart"
            assert _line_count(side_effect) == 1

    asyncio.run(scenario())


def test_approval_interrupt_survives_checkpointer_and_runtime_recreation(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "approval.sqlite"
        side_effect = tmp_path / "approval-side-effects.log"
        thread_id = "approval-proof"

        async with open_langgraph_sqlite(database) as first_handle:
            first_graph = _build_approval_graph(
                side_effect_file=side_effect,
                checkpointer=first_handle.checkpointer,
            )
            waiting = await LangGraphRuntime(first_graph).run(
                RuntimeRequest("task-approval", thread_id, {"prepared": False}, max_steps=20)
            )
            assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL
            assert waiting.resume_token == thread_id
            assert waiting.events[0].payload["value"] == {"question": "approve?"}
            assert _line_count(side_effect) == 1

        async with open_langgraph_sqlite(database) as second_handle:
            second_graph = _build_approval_graph(
                side_effect_file=side_effect,
                checkpointer=second_handle.checkpointer,
            )
            resumed = await LangGraphRuntime(second_graph).resume(
                RuntimeResumeRequest(
                    task_id="task-approval",
                    thread_id=thread_id,
                    resume_token=thread_id,
                    mode=RuntimeResumeMode.APPROVAL,
                    value=True,
                    max_steps=20,
                )
            )
            assert resumed.outcome == RuntimeOutcome.COMPLETED
            assert resumed.output["approved"] is True
            assert resumed.output["result"] == "approved"
            assert _line_count(side_effect) == 1

    asyncio.run(scenario())


def test_corrupt_persisted_checkpoint_fails_closed_instead_of_starting_over(tmp_path) -> None:
    async def create_waiting_thread(database: Path, side_effect: Path) -> None:
        async with open_langgraph_sqlite(database) as handle:
            graph = _build_approval_graph(
                side_effect_file=side_effect,
                checkpointer=handle.checkpointer,
            )
            result = await LangGraphRuntime(graph).run(
                RuntimeRequest("task-corrupt", "corrupt-proof", {"prepared": False}, max_steps=20)
            )
            assert result.outcome == RuntimeOutcome.WAITING_APPROVAL

    async def resume_corrupt_thread(database: Path, side_effect: Path) -> None:
        async with open_langgraph_sqlite(database) as handle:
            graph = _build_approval_graph(
                side_effect_file=side_effect,
                checkpointer=handle.checkpointer,
            )
            result = await LangGraphRuntime(graph).resume(
                RuntimeResumeRequest(
                    task_id="task-corrupt",
                    thread_id="corrupt-proof",
                    resume_token="corrupt-proof",
                    mode=RuntimeResumeMode.APPROVAL,
                    value=True,
                    max_steps=20,
                )
            )
            assert result.outcome == RuntimeOutcome.FAILED
            assert result.error

    database = tmp_path / "corrupt.sqlite"
    side_effect = tmp_path / "corrupt-side-effects.log"
    asyncio.run(create_waiting_thread(database, side_effect))
    assert _line_count(side_effect) == 1

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE checkpoints SET checkpoint = ? WHERE thread_id = ?",
            (b"not-a-valid-msgpack-checkpoint", "corrupt-proof"),
        )
        connection.commit()

    asyncio.run(resume_corrupt_thread(database, side_effect))
    # Failing closed means corruption is surfaced; Nika must not silently restart the graph.
    assert _line_count(side_effect) == 1
