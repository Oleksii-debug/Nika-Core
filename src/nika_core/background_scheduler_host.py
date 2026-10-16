from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from nika_core.background_life import BackgroundWorkKind
from nika_core.background_recurrence import (
    BackgroundEffectResolver,
    BackgroundRecurrenceBridge,
)
from nika_core.background_runtime import BackgroundDispatchGuard
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceState,
)
from nika_core.scheduler.store import ScheduledJobStore

_MAX_IDENTITY_LENGTH = 256


def _required_identity(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    if len(value) > _MAX_IDENTITY_LENGTH:
        raise ValueError(f"{name} is too long")
    return value


class BackgroundSchedulerHost:
    """Own the canonical scheduler/recurrence composition for living-agent background work.

    The host creates no timing, task-state, resource, presence, or effect authority of its own.
    It wires one existing APSchedulerAdapter to one DurableRecurrenceService and delegates the
    actual OWNER_AWAY admission/effect boundary to BackgroundRecurrenceBridge/#855.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        audit: AuditLog,
        guard: BackgroundDispatchGuard,
        effect_resolver: BackgroundEffectResolver,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be exact SQLiteStore")
        if type(audit) is not AuditLog:
            raise TypeError("audit must be exact AuditLog")
        if type(guard) is not BackgroundDispatchGuard:
            raise TypeError("guard must be exact BackgroundDispatchGuard")
        if not callable(effect_resolver):
            raise TypeError("effect_resolver must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self._require_shared_authority(store=store, audit=audit, guard=guard)

        self._jobs = ScheduledJobStore(store)
        self._bridge = BackgroundRecurrenceBridge(
            guard=guard,
            effect_resolver=effect_resolver,
        )
        self._scheduler = APSchedulerAdapter(
            self._jobs,
            self._resolve_scheduler_action,
            audit=audit,
        )
        self._recurrence = DurableRecurrenceService(
            jobs=self._jobs,
            scheduler=self._scheduler,
            handler_resolver=self._bridge.resolve,
            clock=clock,
        )

    def start(self) -> None:
        """Install all enabled durable intents and start the canonical timing engine."""

        self._scheduler.start()

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop the timing engine without altering durable recurrence state."""

        if type(wait) is not bool:
            raise TypeError("wait must be exact built-in bool")
        self._scheduler.shutdown(wait=wait)

    def create(
        self,
        *,
        recurrence_id: str,
        task_id: str,
        work_kind: BackgroundWorkKind,
        effect_action_id: str,
        interval_seconds: int,
        start_at: datetime,
        owner_id: str = "living-agent",
        deadline_at: datetime | None = None,
    ) -> RecurrenceState:
        """Create one durable admission/retry intent for one existing background task."""

        return self._bridge.create(
            self._recurrence,
            recurrence_id=recurrence_id,
            task_id=task_id,
            work_kind=work_kind,
            effect_action_id=effect_action_id,
            interval_seconds=interval_seconds,
            start_at=start_at,
            owner_id=owner_id,
            deadline_at=deadline_at,
        )

    def get(self, recurrence_id: str) -> RecurrenceState | None:
        return self._recurrence.get(_required_identity(recurrence_id, "recurrence_id"))

    def pause(self, recurrence_id: str) -> RecurrenceState:
        return self._recurrence.pause(_required_identity(recurrence_id, "recurrence_id"))

    def resume(self, recurrence_id: str) -> RecurrenceState:
        return self._recurrence.resume(_required_identity(recurrence_id, "recurrence_id"))

    def cancel(self, recurrence_id: str) -> RecurrenceState:
        return self._recurrence.cancel(_required_identity(recurrence_id, "recurrence_id"))

    def runtime_job_installed(self, recurrence_id: str) -> bool:
        """Return whether this durable recurrence has an installed runtime job."""

        recurrence_key = _required_identity(recurrence_id, "recurrence_id")
        matches = []
        for job in self._jobs.list_enabled():
            payload_recurrence_id = job.payload.get("recurrence_id")
            if type(payload_recurrence_id) is not str:
                continue
            if payload_recurrence_id == recurrence_key:
                matches.append(job.job_id)
        if len(matches) > 1:
            raise RuntimeError("multiple enabled scheduler jobs bind the same recurrence")
        return bool(matches and self._scheduler.has_runtime_job(matches[0]))

    def _resolve_scheduler_action(self, action_id: str):
        action_key = _required_identity(action_id, "scheduler action_id")
        if action_key != DurableRecurrenceService.ACTION_ID:
            raise KeyError(f"unknown scheduler action: {action_key}")
        return self._recurrence.action_handler

    @staticmethod
    def _require_shared_authority(
        *,
        store: SQLiteStore,
        audit: AuditLog,
        guard: BackgroundDispatchGuard,
    ) -> None:
        """Reject a split scheduler/effect durable world before any runtime object is created."""

        if audit._store is not store:
            raise ValueError("audit must use the host SQLiteStore instance")
        if guard._audit is not audit:
            raise ValueError("guard and scheduler host must share the exact AuditLog")
        if guard._queue.store is not store:
            raise ValueError("guard TaskQueue must use the host SQLiteStore instance")
        if guard._resources._store is not store:
            raise ValueError("guard ResourceManager must use the host SQLiteStore instance")
