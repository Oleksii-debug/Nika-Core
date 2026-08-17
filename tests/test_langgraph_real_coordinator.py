from __future__ import annotations

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.langgraph_runtime import LangGraphRuntime, open_langgraph_sqlite


class _ApprovalState(TypedDict, total=False):
    prepared: bool
    approved: bool


def _build_graph(checkpointer):
    def prepare(state: _ApprovalState) -> _ApprovalState:
        return {"prepared": True}

    def approve(state: _ApprovalState) -> _ApprovalState:
        approved = interrupt({"question": "allow action?"})
        return {"approved": bool(approved)}

    builder = StateGraph(_ApprovalState)
    builder.add_node("prepare", prepare)
    builder.add_node("approve", approve)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approve")
    builder.add_edge("approve", END)
    return builder.compile(checkpointer=checkpointer)


def _task_state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT state FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return TaskState(row["state"])


def test_real_langgraph_approval_maps_to_persistent_nika_task_and_audit(tmp_path) -> None:
    async def scenario() -> None:
        nika_store = SQLiteStore(tmp_path / "nika.db")
        nika_store.initialize()
        queue = TaskQueue(nika_store)
        audit = AuditLog(nika_store)
        task = queue.create(workspace_id="proof", agent_id="langgraph")
        queue.transition(task.task_id, TaskState.READY)

        checkpoint_db = tmp_path / "langgraph.sqlite"
        thread_id = "coordinator-approval-proof"

        async with open_langgraph_sqlite(checkpoint_db) as first_handle:
            first_runtime = LangGraphRuntime(_build_graph(first_handle.checkpointer))
            first_coordinator = TaskRuntimeCoordinator(queue, audit)
            waiting = await first_coordinator.start(
                first_runtime,
                RuntimeRequest(
                    task_id=task.task_id,
                    thread_id=thread_id,
                    payload={"prepared": False},
                    max_steps=20,
                ),
            )
            assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL
            assert _task_state(nika_store, task.task_id) == TaskState.WAITING_APPROVAL

        # Recreate both the framework runtime/checkpointer and the Nika coordinator objects.
        # The same persisted Nika task and the same LangGraph thread must converge to COMPLETED.
        queue_after_restart = TaskQueue(nika_store)
        audit_after_restart = AuditLog(nika_store)
        async with open_langgraph_sqlite(checkpoint_db) as second_handle:
            second_runtime = LangGraphRuntime(_build_graph(second_handle.checkpointer))
            second_coordinator = TaskRuntimeCoordinator(queue_after_restart, audit_after_restart)
            completed = await second_coordinator.resume_approval(
                second_runtime,
                RuntimeResumeRequest(
                    task_id=task.task_id,
                    thread_id=thread_id,
                    resume_token=thread_id,
                    mode=RuntimeResumeMode.APPROVAL,
                    value=True,
                    max_steps=20,
                ),
            )
            assert completed.outcome == RuntimeOutcome.COMPLETED
            assert completed.output["approved"] is True

        assert _task_state(nika_store, task.task_id) == TaskState.COMPLETED
        events = audit_after_restart.list_for(entity_type="task", entity_id=task.task_id)
        event_types = [event.event_type for event in events]
        assert event_types == [
            "runtime.started",
            "runtime.approval_requested",
            "runtime.finished",
            "runtime.approval_resumed",
            "runtime.finished",
        ]
        assert events[2].payload["outcome"] == RuntimeOutcome.WAITING_APPROVAL.value
        assert events[-1].payload["outcome"] == RuntimeOutcome.COMPLETED.value

    asyncio.run(scenario())
