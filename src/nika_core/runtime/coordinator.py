from __future__ import annotations

from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)


_OUTCOME_TO_STATE: dict[RuntimeOutcome, TaskState] = {
    RuntimeOutcome.COMPLETED: TaskState.COMPLETED,
    RuntimeOutcome.WAITING_APPROVAL: TaskState.WAITING_APPROVAL,
    RuntimeOutcome.PAUSED: TaskState.PAUSED,
    RuntimeOutcome.CANCELLED: TaskState.CANCELLED,
    RuntimeOutcome.FAILED: TaskState.FAILED,
}


class TaskRuntimeCoordinator:
    """Map framework-neutral runtime results into Nika task state and audit history."""

    def __init__(self, queue: TaskQueue, audit: AuditLog) -> None:
        self._queue = queue
        self._audit = audit

    async def start(self, runtime: AgentRuntimePort, request: RuntimeRequest) -> RuntimeResult:
        self._queue.transition(request.task_id, TaskState.RUNNING)
        self._audit.append(
            event_type="runtime.started",
            entity_type="task",
            entity_id=request.task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": request.thread_id},
        )
        try:
            result = await runtime.run(request)
        except Exception as exc:
            result = RuntimeResult(outcome=RuntimeOutcome.FAILED, error=str(exc))
        return self._finish(runtime.runtime_id, request.task_id, request.thread_id, result)

    async def resume_approval(
        self,
        runtime: AgentRuntimePort,
        request: RuntimeResumeRequest,
    ) -> RuntimeResult:
        if request.mode != RuntimeResumeMode.APPROVAL:
            raise ValueError("resume_approval requires APPROVAL mode")
        self._queue.transition(request.task_id, TaskState.RUNNING)
        self._audit.append(
            event_type="runtime.approval_resumed",
            entity_type="task",
            entity_id=request.task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": request.thread_id},
        )
        try:
            result = await runtime.resume(request)
        except Exception as exc:
            result = RuntimeResult(outcome=RuntimeOutcome.FAILED, error=str(exc))
        return self._finish(runtime.runtime_id, request.task_id, request.thread_id, result)

    async def cancel(
        self,
        runtime: AgentRuntimePort,
        *,
        task_id: str,
        thread_id: str,
    ) -> bool:
        self._audit.append(
            event_type="runtime.cancel_requested",
            entity_type="task",
            entity_id=task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": thread_id},
        )
        accepted = await runtime.cancel(task_id=task_id, thread_id=thread_id)
        if not accepted:
            self._audit.append(
                event_type="runtime.cancel_not_active",
                entity_type="task",
                entity_id=task_id,
                payload={"runtime_id": runtime.runtime_id, "thread_id": thread_id},
            )
        return accepted

    def _finish(
        self,
        runtime_id: str,
        task_id: str,
        thread_id: str,
        result: RuntimeResult,
    ) -> RuntimeResult:
        self._queue.transition(task_id, _OUTCOME_TO_STATE[result.outcome])
        for event in result.events:
            self._append_runtime_event(task_id, event)
        self._audit.append(
            event_type="runtime.finished",
            entity_type="task",
            entity_id=task_id,
            payload={
                "runtime_id": runtime_id,
                "thread_id": thread_id,
                "outcome": result.outcome.value,
                "resume_token": result.resume_token,
                "error": result.error,
            },
        )
        return result

    def _append_runtime_event(self, task_id: str, event: RuntimeEvent) -> None:
        self._audit.append(
            event_type=event.event_type,
            entity_type="task",
            entity_id=task_id,
            payload={"sequence": event.sequence, **dict(event.payload)},
        )
