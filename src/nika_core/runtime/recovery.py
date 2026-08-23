from __future__ import annotations

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
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery_claims import recovery_claim_is_reclaimable
from nika_core.runtime.registry import RuntimeRegistry
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
    unresolved external side-effect reservation exists. A recovery claim abandoned before its
    runtime effect starts is reclaimable only after its durable activation lease expires. Once
    effect_started is persisted, restart remains fail-closed until reconciliation. Before any
    execution, the registered runtime must also prove that the persisted resume cursor resolves
    to a readable durable checkpoint.
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
        the runtime-specific async checkpoint preflight. The sync inventory deliberately never
        touches third-party checkpoint objects.
        """
        candidates = tuple(self._classify(record) for record in self._sessions.list_resumable())
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
            probe = await runtime.probe_resume(
                task_id=record.task_id,
                thread_id=record.thread_id,
                resume_token=record.resume_token,
            )
        except Exception as exc:  # noqa: BLE001 - a broken probe must fail closed
            probe = RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.UNREADABLE,
                reason=f"resume checkpoint probe raised: {exc}",
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
                    reason=f"{probe.status.value}: {probe.reason}",
                ),
                probe,
            )
        return candidate, probe

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

        if record.is_active:
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
