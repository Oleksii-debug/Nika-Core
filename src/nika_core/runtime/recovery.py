from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum

from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbePort,
    RuntimeResumeProbeStatus,
    canonical_resume_probe,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery_claims import (
    RECOVERY_RESUME_OPERATION_TYPE,
    recovery_claim_is_reclaimable,
)
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.retry import usable_resume_token
from nika_core.runtime.session_store import RuntimeSessionRecord, RuntimeSessionStore


class RecoveryDisposition(StrEnum):
    """Fail-closed startup decision for one persisted runtime session."""

    AUTO_RESUME_CRASH = "auto_resume_crash"
    WAITING_APPROVAL = "waiting_approval"
    MANUAL_RESUME = "manual_resume"
    RECONCILE_SIDE_EFFECTS = "reconcile_side_effects"
    CHECKPOINT_UNAVAILABLE = "checkpoint_unavailable"
    MISSING_RUNTIME = "missing_runtime"
    INCONSISTENT_STATE = "inconsistent_state"


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    task_id: str
    runtime_id: str
    thread_id: str
    task_state: TaskState | None
    stored_outcome: RuntimeOutcome | None
    disposition: RecoveryDisposition
    reason: str
    unresolved_operation_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryExecution:
    candidate: RecoveryCandidate
    result: RuntimeResult | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.error is None


class RuntimeRecoveryService:
    """Inventory and safely resume runtime work after Nika process recreation.

    Startup recovery is intentionally conservative. Only an ACTIVE session left in RUNNING
    state by abrupt process loss is eligible for automatic continuation, and only when no
    unresolved external side-effect reservation exists. RETRYING is fail-closed because the
    canonical runtime session does not durably encode retry number or not-before authority;
    replaying it after restart could reset bounded backoff or retry budget. A recovery claim
    abandoned before its runtime effect starts is reclaimable only after its durable activation
    lease expires. Once effect_started is persisted, restart remains fail-closed until
    reconciliation. Before any execution, the registered runtime must also prove that the
    persisted resume cursor resolves to a readable durable checkpoint.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue,
        audit: AuditLog,
        runtimes: RuntimeRegistry,
        coordinator: TaskRuntimeCoordinator | None = None,
        sessions: RuntimeSessionStore | None = None,
        idempotency: IdempotencyLedger | None = None,
    ) -> None:
        self._queue = queue
        self._audit = audit
        self._runtimes = runtimes
        self._sessions = sessions or RuntimeSessionStore(queue.store)
        self._coordinator = coordinator or TaskRuntimeCoordinator(
            queue,
            audit,
            session_store=self._sessions,
        )
        self._idempotency = idempotency or IdempotencyLedger(queue.store)

    def inspect(self) -> tuple[RecoveryCandidate, ...]:
        """Return deterministic recovery decisions for every persisted runtime session.

        ACTIVE/RUNNING candidates are provisional until ``resume_safe_crash_sessions`` performs
        the runtime-specific async checkpoint preflight. ACTIVE/RETRYING candidates remain
        fail-closed until canonical durable retry attempt + not-before authority exists.
        Crash-left fresh retries without a runtime session are still surfaced when their latest
        durable retry schedule/start event binds an exact runtime/thread route; they are never
        auto-replayed.
        The sync inventory deliberately never touches third-party checkpoint objects.

        Generic external-effect reservations left PENDING across process recreation have unknown
        outcomes and are promoted to UNCERTAIN. Canonical runtime recovery claims are excluded:
        their CLAIMED/EFFECT_STARTED metadata and lease determine whether recovery is reclaimable
        or must remain fail-closed.
        """
        records = self._sessions.list_resumable()
        orphan_retry_routes = self._orphan_retry_routes()
        inventory_task_ids = dict.fromkeys(
            [record.task_id for record in records]
            + [
                task_id
                for task_id, _task_state, _runtime_id, _thread_id in orphan_retry_routes
            ]
        )
        promoted_operation_keys: list[str] = []
        for task_id in inventory_task_ids:
            pending = self._idempotency.list_for_task(
                task_id,
                status=IdempotencyStatus.PENDING,
            )
            for operation in pending:
                if operation.operation_type == RECOVERY_RESUME_OPERATION_TYPE:
                    continue
                self._idempotency.mark_uncertain(operation.operation_key)
                promoted_operation_keys.append(operation.operation_key)

        candidates = tuple(self._classify(record) for record in records) + tuple(
            self._classify_orphan_retry(
                task_id=task_id,
                task_state=task_state,
                runtime_id=runtime_id,
                thread_id=thread_id,
            )
            for task_id, task_state, runtime_id, thread_id in orphan_retry_routes
        )
        self._audit.append(
            event_type="runtime.recovery_inventory",
            entity_type="runtime_recovery",
            entity_id="startup",
            payload={
                "count": len(candidates),
                "auto_resume_count": sum(
                    item.disposition == RecoveryDisposition.AUTO_RESUME_CRASH
                    for item in candidates
                ),
                "approval_count": sum(
                    item.disposition == RecoveryDisposition.WAITING_APPROVAL
                    for item in candidates
                ),
                "blocked_count": sum(
                    item.disposition
                    in {
                        RecoveryDisposition.RECONCILE_SIDE_EFFECTS,
                        RecoveryDisposition.CHECKPOINT_UNAVAILABLE,
                        RecoveryDisposition.MISSING_RUNTIME,
                        RecoveryDisposition.INCONSISTENT_STATE,
                    }
                    for item in candidates
                ),
                "pending_promoted_count": len(promoted_operation_keys),
                "orphan_retry_count": len(orphan_retry_routes),
            },
        )
        return candidates

    async def resume_safe_crash_sessions(
        self,
        *,
        max_count: int = 4,
        max_steps: int = 64,
        timeout_seconds: float | None = None,
    ) -> tuple[RecoveryExecution, ...]:
        """Resume only crash-left ACTIVE/RUNNING sessions with readable durable checkpoints."""
        if max_count <= 0:
            raise ValueError("max_count must be positive")
        eligible = [
            item
            for item in self.inspect()
            if item.disposition == RecoveryDisposition.AUTO_RESUME_CRASH
        ][:max_count]
        executions: list[RecoveryExecution] = []
        for candidate in eligible:
            try:
                runtime = self._runtimes.get(candidate.runtime_id)
                checked, probe = await self._checkpoint_preflight(candidate, runtime)
                if checked.disposition != RecoveryDisposition.AUTO_RESUME_CRASH:
                    self._audit.append(
                        event_type="runtime.recovery_checkpoint_blocked",
                        entity_type="task",
                        entity_id=candidate.task_id,
                        payload={
                            "runtime_id": candidate.runtime_id,
                            "thread_id": candidate.thread_id,
                            "disposition": checked.disposition.value,
                            "reason": checked.reason,
                            "probe_status": probe.status.value if probe else None,
                            "checkpoint_id": probe.checkpoint_id if probe else None,
                        },
                    )
                    executions.append(
                        RecoveryExecution(candidate=checked, result=None, error=checked.reason)
                    )
                    continue

                self._audit.append(
                    event_type="runtime.recovery_auto_resume_requested",
                    entity_type="task",
                    entity_id=candidate.task_id,
                    payload={
                        "runtime_id": candidate.runtime_id,
                        "thread_id": candidate.thread_id,
                        "checkpoint_id": probe.checkpoint_id if probe else None,
                    },
                )
                result = await self._coordinator.resume_saved(
                    runtime,
                    task_id=candidate.task_id,
                    max_steps=max_steps,
                    timeout_seconds=timeout_seconds,
                )
                executions.append(RecoveryExecution(candidate=checked, result=result))
            except Exception as exc:  # noqa: BLE001 - isolate one failed startup recovery item
                self._audit.append(
                    event_type="runtime.recovery_auto_resume_failed",
                    entity_type="task",
                    entity_id=candidate.task_id,
                    payload={
                        "runtime_id": candidate.runtime_id,
                        "thread_id": candidate.thread_id,
                        "error": str(exc),
                    },
                )
                executions.append(
                    RecoveryExecution(candidate=candidate, result=None, error=str(exc))
                )
        return tuple(executions)

    async def _checkpoint_preflight(
        self,
        candidate: RecoveryCandidate,
        runtime,
    ) -> tuple[RecoveryCandidate, RuntimeResumeProbe | None]:
        record = self._sessions.get(candidate.task_id)
        if record is None:
            return (
                replace(
                    candidate,
                    disposition=RecoveryDisposition.INCONSISTENT_STATE,
                    reason="runtime session disappeared before checkpoint preflight",
                ),
                None,
            )
        if record.runtime_id != candidate.runtime_id or record.thread_id != candidate.thread_id:
            return (
                replace(
                    candidate,
                    disposition=RecoveryDisposition.INCONSISTENT_STATE,
                    reason="runtime session changed before checkpoint preflight",
                ),
                None,
            )
        if usable_resume_token(record.resume_token) is None:
            return (
                replace(
                    candidate,
                    disposition=RecoveryDisposition.INCONSISTENT_STATE,
                    reason="persisted runtime session has no usable resume token",
                ),
                None,
            )
        if not isinstance(runtime, RuntimeResumeProbePort):
            return (
                replace(
                    candidate,
                    disposition=RecoveryDisposition.CHECKPOINT_UNAVAILABLE,
                    reason="runtime cannot prove persisted checkpoint readability",
                ),
                None,
            )

        try:
            probe = canonical_resume_probe(
                await runtime.probe_resume(
                    task_id=record.task_id,
                    thread_id=record.thread_id,
                    resume_token=record.resume_token,
                )
            )
        except Exception:  # noqa: BLE001 - provider diagnostics are untrusted at this boundary
            probe = RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.UNREADABLE,
                reason="checkpoint lookup failed",
            )

        current = self._sessions.get(candidate.task_id)
        if current != record:
            return (
                replace(
                    candidate,
                    disposition=RecoveryDisposition.INCONSISTENT_STATE,
                    reason="runtime session changed during checkpoint preflight",
                ),
                probe,
            )

        if not probe.can_resume:
            return (
                replace(
                    candidate,
                    disposition=RecoveryDisposition.CHECKPOINT_UNAVAILABLE,
                    reason=(
                        "runtime resume checkpoint is not readable: "
                        f"{probe.status.value}"
                    ),
                ),
                probe,
            )
        return candidate, probe

    def _orphan_retry_routes(self) -> tuple[tuple[str, TaskState, str, str], ...]:
        """Return exact durable routes for crash-left fresh retries without a runtime session.

        Fresh retries deliberately do not fabricate a runtime resume cursor. A crash can leave the
        task RETRYING after runtime.retry_scheduled or RUNNING after runtime.retry_started.
        Recovery inventory surfaces either durable route without converting it into replay
        authority.
        """
        routes: list[tuple[str, TaskState, str, str]] = []
        with self._queue.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT tasks.task_id, tasks.state
                FROM tasks
                LEFT JOIN runtime_sessions
                    ON runtime_sessions.task_id = tasks.task_id
                WHERE tasks.state IN (?, ?)
                  AND runtime_sessions.task_id IS NULL
                ORDER BY tasks.task_id
                """,
                (TaskState.RETRYING.value, TaskState.RUNNING.value),
            ).fetchall()
            for task_row in rows:
                task_id = task_row["task_id"]
                if type(task_id) is not str or not task_id.strip():
                    continue
                try:
                    task_state = TaskState(task_row["state"])
                except ValueError:
                    continue
                expected_event = (
                    "runtime.retry_scheduled"
                    if task_state is TaskState.RETRYING
                    else "runtime.retry_started"
                )
                route_row = conn.execute(
                    "SELECT event_type, payload_json FROM audit_events "
                    "WHERE entity_type = ? AND entity_id = ? "
                    "AND event_type IN (?, ?) ORDER BY event_id DESC LIMIT 1",
                    (
                        "task",
                        task_id,
                        "runtime.retry_scheduled",
                        "runtime.retry_started",
                    ),
                ).fetchone()
                if route_row is None or route_row["event_type"] != expected_event:
                    continue
                try:
                    payload = json.loads(route_row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if type(payload) is not dict:
                    continue
                runtime_id = payload.get("runtime_id")
                thread_id = payload.get("thread_id")
                retry_number = payload.get("retry_number")
                if (
                    type(runtime_id) is not str
                    or not runtime_id.strip()
                    or type(thread_id) is not str
                    or not thread_id.strip()
                    or type(retry_number) is not int
                    or retry_number < 1
                ):
                    continue
                routes.append((task_id, task_state, runtime_id, thread_id))
        return tuple(routes)

    def _classify_orphan_retry(
        self,
        *,
        task_id: str,
        task_state: TaskState,
        runtime_id: str,
        thread_id: str,
    ) -> RecoveryCandidate:
        unresolved_records = tuple(
            item
            for item in self._idempotency.list_for_task(task_id)
            if item.status in {IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN}
        )
        unresolved = tuple(item.operation_key for item in unresolved_records)
        if unresolved:
            return RecoveryCandidate(
                task_id=task_id,
                runtime_id=runtime_id,
                thread_id=thread_id,
                task_state=task_state,
                stored_outcome=None,
                disposition=RecoveryDisposition.RECONCILE_SIDE_EFFECTS,
                reason="external side effect is pending or uncertain and must be reconciled first",
                unresolved_operation_keys=unresolved,
            )
        return RecoveryCandidate(
            task_id=task_id,
            runtime_id=runtime_id,
            thread_id=thread_id,
            task_state=task_state,
            stored_outcome=None,
            disposition=RecoveryDisposition.INCONSISTENT_STATE,
            reason=(
                "crash-left fresh retry has durable route evidence but no runtime "
                "session or persisted request/retry-budget authority; automatic replay is unsafe"
            ),
        )

    def _classify(self, record: RuntimeSessionRecord) -> RecoveryCandidate:
        task_state = self._task_state(record.task_id)
        unresolved_records = tuple(
            item
            for item in self._idempotency.list_for_task(record.task_id)
            if item.status in {IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN}
        )
        unresolved = tuple(
            item.operation_key
            for item in unresolved_records
            if not (
                item.status is IdempotencyStatus.PENDING
                and recovery_claim_is_reclaimable(item)
            )
        )

        if unresolved:
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.RECONCILE_SIDE_EFFECTS,
                "external side effect is pending or uncertain and must be reconciled first",
                unresolved,
            )

        try:
            self._runtimes.get(record.runtime_id)
        except KeyError:
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.MISSING_RUNTIME,
                "persisted session runtime is not registered in this process",
            )

        if task_state is None:
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.INCONSISTENT_STATE,
                "persisted runtime session references a missing Nika task",
            )

        if usable_resume_token(record.resume_token) is None:
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.INCONSISTENT_STATE,
                "persisted runtime session has no usable resume token",
            )

        if record.is_active:
            if task_state == TaskState.RETRYING:
                return self._candidate(
                    record,
                    task_state,
                    RecoveryDisposition.INCONSISTENT_STATE,
                    "crash-left RETRYING session has no durable retry attempt/not-before "
                    "authority; automatic resume would reset bounded retry semantics",
                )
            if task_state == TaskState.RUNNING:
                return self._candidate(
                    record,
                    task_state,
                    RecoveryDisposition.AUTO_RESUME_CRASH,
                    "active session with stale RUNNING state indicates abrupt process loss; "
                    "checkpoint preflight and durable recovery claim are required before "
                    "automatic resume",
                )
            if task_state in {TaskState.PAUSED, TaskState.FAILED}:
                return self._candidate(
                    record,
                    task_state,
                    RecoveryDisposition.MANUAL_RESUME,
                    "active session is recoverable but task state requires explicit "
                    "operator intent",
                )
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.INCONSISTENT_STATE,
                "active runtime pointer is incompatible with current task state",
            )

        expected_states = {
            RuntimeOutcome.WAITING_APPROVAL: TaskState.WAITING_APPROVAL,
            RuntimeOutcome.PAUSED: TaskState.PAUSED,
            RuntimeOutcome.FAILED: TaskState.FAILED,
        }
        expected = expected_states.get(record.outcome)
        if expected is None or task_state != expected:
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.INCONSISTENT_STATE,
                "stored runtime outcome does not match current Nika task state",
            )
        if record.outcome == RuntimeOutcome.WAITING_APPROVAL:
            return self._candidate(
                record,
                task_state,
                RecoveryDisposition.WAITING_APPROVAL,
                "human approval value is required before continuation",
            )
        return self._candidate(
            record,
            task_state,
            RecoveryDisposition.MANUAL_RESUME,
            "paused or failed durable work is resumable only by explicit operator action",
        )

    def _task_state(self, task_id: str) -> TaskState | None:
        with self._queue.store.connection() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return None if row is None else TaskState(row["state"])

    @staticmethod
    def _candidate(
        record: RuntimeSessionRecord,
        task_state: TaskState | None,
        disposition: RecoveryDisposition,
        reason: str,
        unresolved_operation_keys: tuple[str, ...] = (),
    ) -> RecoveryCandidate:
        return RecoveryCandidate(
            task_id=record.task_id,
            runtime_id=record.runtime_id,
            thread_id=record.thread_id,
            task_state=task_state,
            stored_outcome=record.outcome,
            disposition=disposition,
            reason=reason,
            unresolved_operation_keys=unresolved_operation_keys,
        )
