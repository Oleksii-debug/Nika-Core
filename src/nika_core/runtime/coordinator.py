from __future__ import annotations

import asyncio
import hashlib
import json

from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition
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
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)
from nika_core.runtime.retry import RetryPolicy
from nika_core.runtime.session_store import (
    RuntimeSessionRecord,
    RuntimeSessionStore,
)

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

_CANCEL_OPERATION_TYPE = "runtime.cancel"


class TaskRuntimeCoordinator:
    """Map framework-neutral runtime results into Nika task state and audit history."""

    def __init__(
        self,
        queue: TaskQueue,
        audit: AuditLog,
        session_store: RuntimeSessionStore | None = None,
        idempotency: IdempotencyLedger | None = None,
    ) -> None:
        self._queue = queue
        self._audit = audit
        self._sessions = session_store or RuntimeSessionStore(queue.store)
        self._idempotency = idempotency or IdempotencyLedger(queue.store)

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
        durable_bound = self._prepare_start(runtime, request)
        self._audit.append(
            event_type="runtime.started",
            entity_type="task",
            entity_id=request.task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": request.thread_id},
        )
        if durable_bound:
            self._audit.append(
                event_type="runtime.session_bound",
                entity_type="task",
                entity_id=request.task_id,
                payload={"runtime_id": runtime.runtime_id, "thread_id": request.thread_id},
            )

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
        if request.value is None:
            raise ValueError("approval decision must be explicit and not None")
        self._require_approval_session(runtime, request)
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
        """Resume durable non-approval work after process recreation."""
        record = self._sessions.get(task_id)
        if record is None:
            raise KeyError(f"No resumable runtime session for task: {task_id}")
        if record.runtime_id != runtime.runtime_id:
            raise ValueError(
                f"Task {task_id} belongs to runtime {record.runtime_id}, not {runtime.runtime_id}"
            )
        if record.outcome == RuntimeOutcome.WAITING_APPROVAL:
            raise ValueError("Persisted approval wait requires explicit resume_saved_approval()")

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

    async def resume_saved_approval(
        self,
        runtime: AgentRuntimePort,
        *,
        task_id: str,
        approval_value,
        max_steps: int = 64,
        timeout_seconds: float | None = None,
    ) -> RuntimeResult:
        if approval_value is None:
            raise ValueError("approval decision must be explicit and not None")
        record = self._sessions.get(task_id)
        if record is None:
            raise KeyError(f"No resumable runtime session for task: {task_id}")
        if record.runtime_id != runtime.runtime_id:
            raise ValueError(
                f"Task {task_id} belongs to runtime {record.runtime_id}, not {runtime.runtime_id}"
            )
        if record.outcome != RuntimeOutcome.WAITING_APPROVAL:
            raise ValueError("Persisted runtime session is not waiting for human approval")
        if self._task_state(task_id) != TaskState.WAITING_APPROVAL:
            raise ValueError("Nika task is not in WAITING_APPROVAL state")

        self._queue.transition(task_id, TaskState.RUNNING)
        self._audit.append(
            event_type="runtime.saved_approval_resumed",
            entity_type="task",
            entity_id=task_id,
            payload={"runtime_id": runtime.runtime_id, "thread_id": record.thread_id},
        )
        result = await self._safe_resume(
            runtime,
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=record.thread_id,
                resume_token=record.resume_token,
                mode=RuntimeResumeMode.APPROVAL,
                value=approval_value,
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
        """Request cancellation without allowing a crash to resurrect the task.

        Cancellation is an external side effect. Nika first commits a PENDING idempotency
        reservation and audit event, then calls the runtime. If the process dies after that
        durable intent but before local finalization, startup recovery sees the unresolved
        operation and refuses automatic resume. An accepted cancellation is then finalized
        atomically with the task state, runtime-session cursor and audit evidence.
        """
        operation_key = self._cancel_operation_key(
            runtime_id=runtime.runtime_id,
            task_id=task_id,
            thread_id=thread_id,
        )
        fingerprint = self._cancel_input_fingerprint(
            runtime_id=runtime.runtime_id,
            task_id=task_id,
            thread_id=thread_id,
        )

        with self._queue.store.connection() as conn:
            reservation, created = self._idempotency.reserve_with_connection(
                conn,
                operation_key=operation_key,
                task_id=task_id,
                operation_type=_CANCEL_OPERATION_TYPE,
                input_fingerprint=fingerprint,
            )
            if not created:
                if reservation.status == IdempotencyStatus.COMPLETED:
                    return bool(reservation.result and reservation.result.get("accepted"))
                raise IdempotencyConflictError(
                    "runtime cancellation is already pending or uncertain; "
                    "reconcile it before replay"
                )
            self._audit.append_with_connection(
                conn,
                event_type="runtime.cancel_requested",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "runtime_id": runtime.runtime_id,
                    "thread_id": thread_id,
                    "operation_key": operation_key,
                },
            )

        try:
            accepted = await runtime.cancel(task_id=task_id, thread_id=thread_id)
        except Exception as exc:
            with self._queue.store.connection() as conn:
                self._idempotency.mark_uncertain_with_connection(conn, operation_key)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.cancel_uncertain",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "runtime_id": runtime.runtime_id,
                        "thread_id": thread_id,
                        "operation_key": operation_key,
                        "error": str(exc),
                    },
                )
            raise

        if not accepted:
            with self._queue.store.connection() as conn:
                self._idempotency.release_pending_with_connection(conn, operation_key)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.cancel_not_active",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "runtime_id": runtime.runtime_id,
                        "thread_id": thread_id,
                        "operation_key": operation_key,
                    },
                )
            return False

        with self._queue.store.connection() as conn:
            current = self._task_state_with_connection(conn, task_id)
            state_changed = False
            if current != TaskState.CANCELLED and can_transition(current, TaskState.CANCELLED):
                self._queue.transition_with_connection(conn, task_id, TaskState.CANCELLED)
                state_changed = True
            elif current not in {
                TaskState.CANCELLED,
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.ARCHIVED,
            }:
                raise ValueError(
                    f"Accepted runtime cancellation cannot be reconciled from task state {current.value}"
                )

            self._sessions.delete_with_connection(conn, task_id)
            self._idempotency.complete_with_connection(
                conn,
                operation_key,
                {
                    "accepted": True,
                    "task_state": (
                        TaskState.CANCELLED.value if state_changed else current.value
                    ),
                },
            )
            self._audit.append_with_connection(
                conn,
                event_type="runtime.cancel_accepted",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "runtime_id": runtime.runtime_id,
                    "thread_id": thread_id,
                    "operation_key": operation_key,
                    "previous_task_state": current.value,
                    "task_state_changed": state_changed,
                },
            )
        return True

    def _require_approval_session(
        self,
        runtime: AgentRuntimePort,
        request: RuntimeResumeRequest,
    ) -> RuntimeSessionRecord:
        if self._task_state(request.task_id) != TaskState.WAITING_APPROVAL:
            raise ValueError("Nika task is not in WAITING_APPROVAL state")
        record = self._sessions.get(request.task_id)
        if record is None:
            raise KeyError(f"No resumable runtime session for task: {request.task_id}")
        if record.outcome != RuntimeOutcome.WAITING_APPROVAL:
            raise ValueError("Persisted runtime session is not waiting for human approval")
        if record.runtime_id != runtime.runtime_id:
            raise ValueError(
                f"Task {request.task_id} belongs to runtime {record.runtime_id}, "
                f"not {runtime.runtime_id}"
            )
        if record.thread_id != request.thread_id:
            raise ValueError("Approval request thread does not match persisted runtime session")
        if record.resume_token != request.resume_token:
            raise ValueError("Approval resume token does not match persisted runtime session")
        return record

    def _prepare_start(self, runtime: AgentRuntimePort, request: RuntimeRequest) -> bool:
        """Start task state and durable routing cursor in one Nika SQLite transaction.

        Durable runtimes may expose ``initial_resume_token``. In that case the READY->RUNNING
        event and ACTIVE runtime-session pointer commit together, so process loss cannot leave
        durable RUNNING work without its Nika recovery cursor. Existing cursors are never
        overwritten by a fresh start; callers must use an explicit resume path.
        """
        token_factory = getattr(runtime, "initial_resume_token", None)
        if not callable(token_factory):
            self._queue.transition(request.task_id, TaskState.RUNNING)
            return False
        resume_token = token_factory(task_id=request.task_id, thread_id=request.thread_id)
        if not resume_token:
            self._queue.transition(request.task_id, TaskState.RUNNING)
            return False

        with self._queue.store.connection() as conn:
            self._queue.transition_with_connection(conn, request.task_id, TaskState.RUNNING)
            self._sessions.record_active_with_connection(
                conn,
                task_id=request.task_id,
                runtime_id=runtime.runtime_id,
                thread_id=request.thread_id,
                resume_token=str(resume_token),
            )
        return True

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
            self._queue.transition(task_id, TaskState.READY)
            self._queue.transition(task_id, TaskState.RUNNING)
            self._audit.append(
                event_type="runtime.crash_recovery_started",
                entity_type="task",
                entity_id=task_id,
                payload={"runtime_id": record.runtime_id, "thread_id": record.thread_id},
            )
            return RuntimeResumeMode.CONTINUE

        if record.outcome in {RuntimeOutcome.PAUSED, RuntimeOutcome.FAILED}:
            self._queue.transition(task_id, TaskState.READY)
            self._queue.transition(task_id, TaskState.RUNNING)
            return RuntimeResumeMode.CONTINUE
        raise ValueError(f"Stored runtime outcome is not resumable: {record.outcome}")

    def _task_state(self, task_id: str) -> TaskState:
        with self._queue.store.connection() as conn:
            return self._task_state_with_connection(conn, task_id)

    @staticmethod
    def _task_state_with_connection(conn, task_id: str) -> TaskState:
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
        except Exception as exc:  # noqa: BLE001 - runtime adapter boundary normalizes failures
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
        except Exception as exc:  # noqa: BLE001 - runtime adapter boundary normalizes failures
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
        """Commit result cursor, task state and audit evidence as one local transaction.

        The runtime may already have durably checkpointed its own execution result. Nika must
        therefore never expose a partially finalized local product state. If session mutation,
        task transition or audit serialization/write fails, the whole Nika transaction rolls
        back and the previous recovery cursor/state remain available for explicit recovery.

        A previously committed CANCELLED task is authoritative human intent. If an in-flight
        runtime coroutine races with accepted cancellation and reports a later outcome, Nika
        records that observation but does not resurrect or overwrite the cancelled task.
        """
        with self._queue.store.connection() as conn:
            current = self._task_state_with_connection(conn, task_id)
            cancellation_won = current == TaskState.CANCELLED

            if cancellation_won:
                self._sessions.delete_with_connection(conn, task_id)
            elif result.outcome in _RESUMABLE_OUTCOMES and result.resume_token:
                self._sessions.record_result_with_connection(
                    conn,
                    task_id=task_id,
                    runtime_id=runtime_id,
                    thread_id=thread_id,
                    result=result,
                )
            else:
                self._sessions.delete_with_connection(conn, task_id)

            if not cancellation_won:
                self._queue.transition_with_connection(
                    conn,
                    task_id,
                    _OUTCOME_TO_STATE[result.outcome],
                )

            for event in result.events:
                self._append_runtime_event_with_connection(conn, task_id, event)
            if cancellation_won and result.outcome != RuntimeOutcome.CANCELLED:
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.finished_after_cancel",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "runtime_id": runtime_id,
                        "thread_id": thread_id,
                        "runtime_outcome": result.outcome.value,
                    },
                )
            self._audit.append_with_connection(
                conn,
                event_type="runtime.finished",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "runtime_id": runtime_id,
                    "thread_id": thread_id,
                    "outcome": (
                        RuntimeOutcome.CANCELLED.value
                        if cancellation_won
                        else result.outcome.value
                    ),
                    "runtime_reported_outcome": result.outcome.value,
                    "resume_token": result.resume_token,
                    "error": result.error,
                    "error_code": result.error_code.value if result.error_code else None,
                },
            )
        if cancellation_won and result.outcome != RuntimeOutcome.CANCELLED:
            return RuntimeResult(
                outcome=RuntimeOutcome.CANCELLED,
                events=result.events,
                output=result.output,
            )
        return result

    def _append_runtime_event_with_connection(self, conn, task_id: str, event: RuntimeEvent) -> None:
        self._audit.append_with_connection(
            conn,
            event_type=event.event_type,
            entity_type="task",
            entity_id=task_id,
            payload={"sequence": event.sequence, **dict(event.payload)},
        )

    @staticmethod
    def _cancel_operation_key(*, runtime_id: str, task_id: str, thread_id: str) -> str:
        material = json.dumps(
            {"runtime_id": runtime_id, "task_id": task_id, "thread_id": thread_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"runtime.cancel:{hashlib.sha256(material).hexdigest()}"

    @staticmethod
    def _cancel_input_fingerprint(*, runtime_id: str, task_id: str, thread_id: str) -> str:
        material = json.dumps(
            {
                "operation": _CANCEL_OPERATION_TYPE,
                "runtime_id": runtime_id,
                "task_id": task_id,
                "thread_id": thread_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()
