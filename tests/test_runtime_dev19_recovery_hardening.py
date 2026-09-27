from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.runtime.langgraph_runtime import LangGraphRuntime, open_langgraph_sqlite
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


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


def _build_failure_then_resume_graph(*, side_effect: Path, failure_marker: Path, checkpointer):
    def prepare(state: _RuntimeState) -> _RuntimeState:
        _append_line(side_effect, "prepared")
        return {"prepared": True}

    def flaky_finish(state: _RuntimeState) -> _RuntimeState:
        if not failure_marker.exists():
            failure_marker.write_text("failed-once", encoding="utf-8")
            raise RuntimeError("intentional failure after completed effect")
        return {"result": "completed-after-recovery"}

    builder = StateGraph(_RuntimeState)
    builder.add_node("prepare", prepare)
    builder.add_node("finish", flaky_finish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


def _build_approval_graph(*, side_effect: Path, checkpointer):
    def prepare(state: _RuntimeState) -> _RuntimeState:
        _append_line(side_effect, "prepared")
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


def _build_side_effect_graph(*, side_effect: Path, checkpointer):
    def effect(state: _RuntimeState) -> _RuntimeState:
        _append_line(side_effect, "executed")
        return {"result": "done"}

    builder = StateGraph(_RuntimeState)
    builder.add_node("effect", effect)
    builder.add_edge(START, "effect")
    builder.add_edge("effect", END)
    return builder.compile(checkpointer=checkpointer)


def _running_nika_task(store: SQLiteStore, *, thread_id: str) -> tuple[TaskQueue, str]:
    queue = TaskQueue(store)
    task = queue.create(workspace_id="runtime-proof", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="langgraph",
        thread_id=thread_id,
        resume_token=thread_id,
    )
    return queue, task.task_id


def _task_state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"])


def test_startup_recovery_resumes_completed_effect_without_repeat(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime_db = tmp_path / "runtime.sqlite"
        nika_store = SQLiteStore(tmp_path / "nika.db")
        nika_store.initialize()
        side_effect = tmp_path / "side-effect.log"
        failure_marker = tmp_path / "failure.marker"
        thread_id = "effect-crash-window"

        async with open_langgraph_sqlite(runtime_db) as first_handle:
            first_graph = _build_failure_then_resume_graph(
                side_effect=side_effect,
                failure_marker=failure_marker,
                checkpointer=first_handle.checkpointer,
            )
            first_result = await LangGraphRuntime(first_graph).run(
                RuntimeRequest("framework-task", thread_id, {"prepared": False}, max_steps=20)
            )
            assert first_result.outcome == RuntimeOutcome.FAILED
            assert first_result.resume_token == thread_id
            assert _line_count(side_effect) == 1

        queue, task_id = _running_nika_task(nika_store, thread_id=thread_id)

        async with open_langgraph_sqlite(runtime_db) as second_handle:
            second_graph = _build_failure_then_resume_graph(
                side_effect=side_effect,
                failure_marker=failure_marker,
                checkpointer=second_handle.checkpointer,
            )
            runtime = LangGraphRuntime(second_graph)
            registry = RuntimeRegistry()
            registry.register(runtime)
            recovery = RuntimeRecoveryService(
                queue=queue,
                audit=AuditLog(nika_store),
                runtimes=registry,
            )

            executions = await recovery.resume_safe_crash_sessions(max_count=4, max_steps=20)

        assert len(executions) == 1
        assert executions[0].succeeded
        assert executions[0].result is not None
        assert executions[0].result.output["result"] == "completed-after-recovery"
        assert _line_count(side_effect) == 1
        assert _task_state(nika_store, task_id) == TaskState.COMPLETED
        assert RuntimeSessionStore(nika_store).get(task_id) is None

    asyncio.run(scenario())


def test_startup_recovery_blocks_missing_checkpoint_without_invoking_graph(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime_db = tmp_path / "missing-runtime.sqlite"
        nika_store = SQLiteStore(tmp_path / "missing-nika.db")
        nika_store.initialize()
        side_effect = tmp_path / "missing-side-effect.log"
        thread_id = "missing-checkpoint"
        queue, task_id = _running_nika_task(nika_store, thread_id=thread_id)

        async with open_langgraph_sqlite(runtime_db) as handle:
            graph = _build_side_effect_graph(
                side_effect=side_effect,
                checkpointer=handle.checkpointer,
            )
            registry = RuntimeRegistry()
            registry.register(LangGraphRuntime(graph))
            recovery = RuntimeRecoveryService(
                queue=queue,
                audit=AuditLog(nika_store),
                runtimes=registry,
            )

            executions = await recovery.resume_safe_crash_sessions(max_count=4)

        assert len(executions) == 1
        assert not executions[0].succeeded
        assert executions[0].candidate.disposition == RecoveryDisposition.CHECKPOINT_UNAVAILABLE
        assert "missing" in executions[0].candidate.reason
        assert _line_count(side_effect) == 0
        assert _task_state(nika_store, task_id) == TaskState.RUNNING
        assert RuntimeSessionStore(nika_store).get(task_id) is not None
        event_types = [
            event.event_type
            for event in AuditLog(nika_store).list_for(entity_type="task", entity_id=task_id)
        ]
        assert "runtime.recovery_checkpoint_blocked" in event_types
        assert "runtime.recovery_auto_resume_requested" not in event_types

    asyncio.run(scenario())


def test_startup_recovery_blocks_corrupt_checkpoint_without_replay(tmp_path: Path) -> None:
    async def create_checkpoint(runtime_db: Path, side_effect: Path, thread_id: str) -> None:
        async with open_langgraph_sqlite(runtime_db) as handle:
            graph = _build_approval_graph(
                side_effect=side_effect,
                checkpointer=handle.checkpointer,
            )
            waiting = await LangGraphRuntime(graph).run(
                RuntimeRequest("framework-corrupt", thread_id, {"prepared": False}, max_steps=20)
            )
            assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL

    async def attempt_recovery(
        runtime_db: Path,
        nika_store: SQLiteStore,
        queue: TaskQueue,
        side_effect: Path,
    ):
        async with open_langgraph_sqlite(runtime_db) as handle:
            graph = _build_approval_graph(
                side_effect=side_effect,
                checkpointer=handle.checkpointer,
            )
            registry = RuntimeRegistry()
            registry.register(LangGraphRuntime(graph))
            recovery = RuntimeRecoveryService(
                queue=queue,
                audit=AuditLog(nika_store),
                runtimes=registry,
            )
            return await recovery.resume_safe_crash_sessions(max_count=4)

    runtime_db = tmp_path / "corrupt-runtime.sqlite"
    nika_store = SQLiteStore(tmp_path / "corrupt-nika.db")
    nika_store.initialize()
    side_effect = tmp_path / "corrupt-side-effect.log"
    thread_id = "corrupt-checkpoint"

    asyncio.run(create_checkpoint(runtime_db, side_effect, thread_id))
    assert _line_count(side_effect) == 1

    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "UPDATE checkpoints SET checkpoint = ? WHERE thread_id = ?",
            (b"not-a-valid-msgpack-checkpoint", thread_id),
        )
        connection.commit()

    queue, task_id = _running_nika_task(nika_store, thread_id=thread_id)
    executions = asyncio.run(attempt_recovery(runtime_db, nika_store, queue, side_effect))

    assert len(executions) == 1
    assert not executions[0].succeeded
    assert executions[0].candidate.disposition == RecoveryDisposition.CHECKPOINT_UNAVAILABLE
    assert "unreadable" in executions[0].candidate.reason
    assert _line_count(side_effect) == 1
    assert _task_state(nika_store, task_id) == TaskState.RUNNING
    assert RuntimeSessionStore(nika_store).get(task_id) is not None
