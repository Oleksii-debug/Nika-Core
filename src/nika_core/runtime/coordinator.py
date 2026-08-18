from __future__ import annotations

import asyncio

from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeErrorCode,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)
from nika_core.runtime.retry import RetryPolicy
from nika_core.runtime.session_store import RuntimeSessionRecord, RuntimeSessionStore


_OUTCOME_TO_STATE: dict[RuntimeOutcome, TaskState] = {
    RuntimeOutcome.COMPLETED: TaskState.COMPLETED,
    RuntimeOutcome.WAITING_APPROVAL: TaskState.WAITING_APPROVAL,
    RuntimeOutcome.PAUSED: TaskState.PAUSED,
    RuntimeOutcome.CANCELLED: TaskState.CANCELLED,
    RuntimeOutcome.FAILED: TaskState.FAILED,
}

_RESUMABLE_OUTCOMES = frozenset(
    {
        RuntimeOutcome.WAITING_APPROVAL,
        RuntimeOutcome.PAUSED,
        RuntimeOutcome.FAILED,
    }
)


class TaskRuntimeCoordinator:
    """Map framework-neutral runtime results into Nika task state and audit history."""

    def __init__(
        self,
        queue: TaskQueue,
        audit: AuditLog,
        session_store: RuntimeSessionStore | None = None,
    ) -> None:
        self._queue = queue
        self._audit = audit
        self._sessions = session_store or RuntimeSessionStore(queue.store)

    @property
    def sessions(self) -> RuntimeSessionStore:
        return self._sessions

    async def start(
        self,
        runtime: AgentRuntimePort,
        request: RuntimeRequest,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> RuntimeResult:
        policy = retry_policy or RetryPolicy()
        self._queue.transition(request.task_id, TaskState.RUNNING)
        self._audit.append(
            event_type="runtime.started",
            entity_type="task",
            entity_id=request.task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": request.thread_id},
        )
        self._bind_active_session(runtime, request)

        result = await self._safe_run(runtime, request)
        retries_used = 0
        while policy.should_retry(result, retries_used=retries_used):
            retries_used += 1
            delay = policy.delay_seconds(retry_number=retries_used)
            self._queue.transition(request.task_id, TaskState.RETRYING)
            self._audit.append(
                event_type="runtime.retry_scheduled",
                entity_type="task",
                entity_id=request.task_id,
                payload={
                    "runtime_id": runtime.runtime_id,
                    "thread_id": request.thread_id,
                    "retry_number": retries_used,
                    "delay_seconds": delay,
                    "error": result.error,
                    "error_code": result.error_code.value if result.error_code else None,
                    "resume_token": result.resume_token,
                },
            )
            if delay:
                await asyncio.sleep(delay)
            self._queue.transition(request.task_id, TaskState.RUNNING)
            self._audit.append(
                event_type="runtime.retry_started",
                entity_type="task",
                entity_id=request.task_id,
                payload={
                    "runtime_id": runtime.runtime_id,
                    "thread_id": request.thread_id,
                    "retry_number": retries_used,
                    "resume": result.resume_token is not None,
                },
            )
            if result.resume_token is not None:
                result = await self._safe_resume(
                    runtime,
                    RuntimeResumeRequest(
                        task_id=request.task_id,
                        thread_id=request.thread_id,
                        resume_token=result.resume_token,
                        mode=RuntimeResumeMode.CONTINUE,
                        max_steps=request.max_steps,
                        timeout_seconds=request.timeout_seconds,
                    ),
                )
            else:
                result = await self._safe_run(runtime, request)

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
        result = await self._safe_resume(runtime, request)
        return self._finish(runtime.runtime_id, request.task_id, request.thread_id, result)

    async def resume_saved(
        self,
        runtime: AgentRuntimePort,
        *,
        task_id: str,
        value=None,
        max_steps: int = 64,
        timeout_seconds: float | None = None,
    ) -> RuntimeResult:
        """Resume durable work after process recreation using only its Nika task ID."""
        record = self._sessions.get(task_id)
        if record is None:
            raise KeyError(f"No resumable runtime session for task: {task_id}")
        if record.runtime_id != runtime.runtime_id:
            raise ValueError(
                f"Task {task_id} belongs to runtime {record.runtime_id}, not {runtime.runtime_id}"
            )

        mode = self._prepare_saved_resume_state(task_id, record)
        self._audit.append(
            event_type="runtime.saved_resume_started",
            entity_type="task",
            entity_id=task_id,
            payload={
                "runtime_id": runtime.runtime_id,
                "thread_id": record.thread_id,
                "stored_outcome": record.outcome.value if record.outcome else "active",
                "mode": mode.value,
            },
        )
        result = await self._safe_resume(
            runtime,
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=record.thread_id,
                resume_token=record.resume_token,
                mode=mode,
                value=value,
                max_steps=max_steps,
                timeout_seconds=timeout_seconds,
            ),
        )
        return self._finish(runtime.runtime_id, task_id, record.thread_id, result)

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

    def _bind_active_session(self, runtime: AgentRuntimePort, request: RuntimeRequest) -> None:
        """Persist a pre-run durable cursor when the runtime can provide one.

        This record intentionally exists before awaiting the runtime. If the Python process
        disappears after LangGraph has persisted a checkpoint but before it returns a result,
        the next Nika process can still recover the thread from only the Nika task ID.
        """
        token_factory = getattr(runtime, "initial_resume_token", None)
        if not callable(token_factory):
            return
        resume_token = token_factory(task_id=request.task_id, thread_id=request.thread_id)
        if not resume_token:
            return
        self._sessions.record_active(
            task_id=request.task_id,
            runtime_id=runtime.runtime_id,
            thread_id=request.thread_id,
            resume_token=str(resume_token),
        )
        self._audit.append(
            event_type="runtime.session_bound",
            entity_type="task",
            entity_id=request.task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": request.thread_id},
        )

    def _prepare_saved_resume_state(
        self,
        task_id: str,
        record: RuntimeSessionRecord,
    ) -> RuntimeResumeMode:
        if record.is_active:
            current = self._task_state(task_id)
            if current == TaskState.RUNNING:
                self._queue.transition(task_id, TaskState.PAUSED)
            elif current not in {TaskState.PAUSED, TaskState.FAILED}:
                raise ValueError(
                    f"Active runtime session has incompatible task state: {current.value}"
                )
            if self._task_state(task_id) == TaskState.FAILED:
                self._queue.transition(task_id, TaskState.READY)
            else:
                self._queue.transition(task_id, TaskState.READY)
            self._queue.transition(task_id, TaskState.RUNNING)
            self._audit.append(
                event_type="runtime.crash_recovery_started",
                entity_type="task",
                entity_id=task_id,
                payload={"runtime_id": record.runtime_id, "thread_id": record.thread_id},
            )
            return RuntimeResumeMode.CONTINUE

        if record.outcome == RuntimeOutcome.WAITING_APPROVAL:
            self._queue.transition(task_id, TaskState.RUNNING)
            return RuntimeResumeMode.APPROVAL
        if record.outcome in {RuntimeOutcome.PAUSED, RuntimeOutcome.FAILED}:
            self._queue.transition(task_id, TaskState.READY)
            self._queue.transition(task_id, TaskState.RUNNING)
            return RuntimeResumeMode.CONTINUE
        raise ValueError(f"Stored runtime outcome is not resumable: {record.outcome}")

    def _task_state(self, task_id: str) -> TaskState:
        with self._queue.store.connection() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return TaskState(row["state"])

    async def _safe_run(
        self,
        runtime: AgentRuntimePort,
        request: RuntimeRequest,
    ) -> RuntimeResult:
        try:
            return await runtime.run(request)
        except Exception as exc:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error=str(exc),
                error_code=RuntimeErrorCode.INTERNAL,
            )

    async def _safe_resume(
        self,
        runtime: AgentRuntimePort,
        request: RuntimeResumeRequest,
    ) -> RuntimeResult:
        try:
            return await runtime.resume(request)
        except Exception as exc:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error=str(exc),
                error_code=RuntimeErrorCode.INTERNAL,
            )

    def _finish(
        self,
        runtime_id: str,
        task_id: str,
        thread_id: str,
        result: RuntimeResult,
    ) -> RuntimeResult:
        if result.outcome in _RESUMABLE_OUTCOMES and result.resume_token:
            self._sessions.record_result(
                task_id=task_id,
                runtime_id=runtime_id,
                thread_id=thread_id,
                result=result,
            )

        self._queue.transition(task_id, _OUTCOME_TO_STATE[result.outcome])

        if result.outcome not in _RESUMABLE_OUTCOMES or not result.resume_token:
            self._sessions.delete(task_id)

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
                "error_code": result.error_code.value if result.error_code else None,
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
