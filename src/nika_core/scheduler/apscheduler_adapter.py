from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.contracts import ScheduledJob, SchedulerPort, TriggerKind
from nika_core.scheduler.store import ScheduledJobStore

ActionHandler = Callable[[dict[str, Any]], None]
HandlerResolver = Callable[[str], ActionHandler]
_TERMINAL_TASK_STATES = frozenset(
    {TaskState.CANCELLED, TaskState.COMPLETED, TaskState.ARCHIVED}
)
_SYNC_RETRY_LIMIT = 3


class APSchedulerAdapter(SchedulerPort):
    def __init__(
        self,
        jobs: ScheduledJobStore,
        handler_resolver: HandlerResolver,
        *,
        audit: AuditLog | None = None,
    ) -> None:
        self._jobs = jobs
        self._handler_resolver = handler_resolver
        self._audit = audit
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._started = False
        self._starting = False
        self._lifecycle_lock = RLock()
        self._runtime_sync_lock = RLock()
        self._shutdown_requested = False
        self._shutdown_wait = True

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._starting:
                return
            self._starting = True
            self._shutdown_requested = False
            self._shutdown_wait = True
            try:
                try:
                    for job in self._jobs.list_enabled():
                        self._sync_runtime_job(job.job_id)
                except Exception:
                    self._scheduler = BackgroundScheduler(timezone="UTC")
                    raise
                if self._shutdown_requested:
                    self._scheduler = BackgroundScheduler(timezone="UTC")
                    return
                try:
                    self._scheduler.start()
                except Exception:
                    self._scheduler = BackgroundScheduler(timezone="UTC")
                    raise
                if self._shutdown_requested:
                    replacement = BackgroundScheduler(timezone="UTC")
                    self._scheduler.shutdown(wait=self._shutdown_wait)
                    self._scheduler = replacement
                    return
                self._started = True
            finally:
                self._starting = False
                self._shutdown_requested = False
                self._shutdown_wait = True

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lifecycle_lock:
            if self._starting:
                self._shutdown_requested = True
                self._shutdown_wait = wait
                return
            if not self._started:
                return
            replacement = BackgroundScheduler(timezone="UTC")
            self._scheduler.shutdown(wait=wait)
            self._scheduler = replacement
            self._started = False

    def upsert(self, job: ScheduledJob) -> None:
        if type(job) is not ScheduledJob:
            raise TypeError("job must be an exact ScheduledJob")
        job_id = _require_job_id(job.job_id)
        self._jobs.upsert(job)
        effective_job = self._required_job(job_id)
        if self._started or self._starting:
            self._sync_runtime_job(job_id)
            effective_job = self._required_job(job_id)
        elif not self._task_authority_allows(effective_job):
            effective_job = self._required_job(job_id)
        self._audit_change("scheduler.job_upserted", effective_job)

    def activate_persisted(self, job: ScheduledJob) -> None:
        """Reconcile an already-durable job without overwriting newer SQLite state.

        ConnectivityWaitService atomically commits its task, job and audit before
        runtime installation. Calling upsert() here would write a stale snapshot
        if another process replaced the job between commit and activation.
        The existing runtime synchronizer reads the latest durable authority
        and installs only that snapshot; start() rehydrates when not running.
        """
        if type(job) is not ScheduledJob:
            raise TypeError("job must be an exact ScheduledJob")
        job_id = _require_job_id(job.job_id)
        if self._started or self._starting:
            self._sync_runtime_job(job_id)

    def remove(self, job_id: str) -> bool:
        job_id = _require_job_id(job_id)
        removed = self._jobs.delete(job_id)
        if self._started or self._starting:
            self._sync_runtime_job(job_id)
        remaining = self._jobs.get(job_id)
        if removed and remaining is None and self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_removed",
                entity_type="scheduled_job",
                entity_id=job_id,
            )
        return removed

    def pause(self, job_id: str) -> None:
        job = self._required_job(_require_job_id(job_id))
        self._jobs.set_enabled(job.job_id, False)
        if self._started or self._starting:
            self._sync_runtime_job(job.job_id)
        paused_job = self._required_job(job.job_id)
        if paused_job.enabled:
            return
        self._audit_change("scheduler.job_paused", paused_job)

    def resume(self, job_id: str) -> None:
        job = self._required_job(_require_job_id(job_id))
        self._jobs.set_enabled(job.job_id, True)
        enabled_job = self._required_job(job.job_id)
        if not enabled_job.enabled:
            return
        if self._started or self._starting:
            installed_job = self._sync_runtime_job(enabled_job.job_id)
            if installed_job is None:
                return
            enabled_job = installed_job
        elif not self._task_authority_allows(enabled_job):
            return
        self._audit_change("scheduler.job_resumed", enabled_job)

    def has_runtime_job(self, job_id: str) -> bool:
        job_id = _require_job_id(job_id)
        return self._scheduler.get_job(job_id) is not None

    def _sync_runtime_job(self, job_id: str) -> ScheduledJob | None:
        job_id = _require_job_id(job_id)
        # Durable writers may commit while another caller reconciles live APScheduler
        # state. Serialize only live reconciliation so an older in-flight install
        # cannot overwrite the runtime state of a newer canonical adapter mutation.
        with self._runtime_sync_lock:
            for _ in range(_SYNC_RETRY_LIMIT):
                job = self._jobs.get(job_id)
                if job is None or not job.enabled:
                    self._remove_runtime_job(job_id)
                    return None
                if not self._task_authority_allows(job):
                    current = self._jobs.get(job_id)
                    if current is not None and current != job:
                        continue
                    self._remove_runtime_job(job_id)
                    return None
                current = self._jobs.get(job_id)
                if current is None or not current.enabled:
                    self._remove_runtime_job(job_id)
                    return None
                if current != job:
                    continue
                if not self._task_authority_allows(current):
                    after_authority = self._jobs.get(job_id)
                    if after_authority is not None and after_authority != current:
                        continue
                    self._remove_runtime_job(job_id)
                    return None
                if self._jobs.get(job_id) != current:
                    continue
                try:
                    self._install(current)
                except Exception:
                    self._remove_runtime_job(job_id)
                    raise
                return current
            self._remove_runtime_job(job_id)
            return None

    def _remove_runtime_job(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)

    def _install(self, job: ScheduledJob) -> None:
        self._scheduler.add_job(
            self._dispatch,
            trigger=_make_trigger(job),
            id=job.job_id,
            args=(job.job_id, job),
            replace_existing=True,
            coalesce=job.coalesce,
            max_instances=job.max_instances,
            misfire_grace_time=job.misfire_grace_seconds,
        )

    def _dispatch(
        self,
        job_id: str,
        installed_job: ScheduledJob | None = None,
    ) -> None:
        job_id = _require_job_id(job_id)
        if installed_job is not None:
            if type(installed_job) is not ScheduledJob:
                raise TypeError("installed_job must be an exact ScheduledJob or None")
            if _require_job_id(installed_job.job_id) != job_id:
                return
        job = self._jobs.get(job_id)
        if job is None or not job.enabled:
            return
        if installed_job is not None and job != installed_job:
            return
        if not self._task_authority_allows(job):
            if self._started or self._starting:
                self._sync_runtime_job(job_id)
            return
        # Linearize durable scheduler + task authority before touching the
        # resolver or any external handler surface. A stale occurrence that lost
        # authority must have zero resolver/effect observations.
        dispatch_snapshot = installed_job if installed_job is not None else job
        job = self._jobs.authorize_dispatch(dispatch_snapshot)
        if job is None:
            if self._started or self._starting:
                self._sync_runtime_job(job_id)
            else:
                current = self._jobs.get(job_id)
                if current is not None:
                    self._task_authority_allows(current)
            return
        action_id = job.action_id
        try:
            handler = self._handler_resolver(action_id)
        except Exception as exc:
            self._audit_failure(job_id, action_id, exc)
            raise
        if self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_started",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload={"action_id": job.action_id},
            )
        try:
            handler(dict(job.payload))
        except Exception as exc:
            self._audit_failure(job_id, job.action_id, exc)
            raise
        if self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_completed",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload={"action_id": job.action_id},
            )

    def _task_authority_allows(self, job: ScheduledJob) -> bool:
        if "task_id" not in job.payload:
            return True
        task_id = job.payload["task_id"]
        if not isinstance(task_id, str) or not task_id or task_id != task_id.strip():
            self._suppress_task_linked_job(job, reason="invalid_task_binding")
            return False
        task_state = self._jobs.task_state(task_id)
        if task_state is None:
            self._suppress_task_linked_job(job, reason="missing_task")
            return False
        if task_state in _TERMINAL_TASK_STATES:
            self._suppress_task_linked_job(
                job,
                reason="terminal_task",
                task_state=task_state,
            )
            return False
        return True

    def _suppress_task_linked_job(
        self,
        job: ScheduledJob,
        *,
        reason: str,
        task_state: TaskState | None = None,
    ) -> None:
        if not self._jobs.disable_if_current(job):
            return
        if self._audit is not None:
            payload: dict[str, Any] = {
                "action_id": job.action_id,
                "reason": reason,
            }
            if task_state is not None:
                payload["task_state"] = task_state.value
            self._audit.append(
                event_type="scheduler.job_suppressed_task_authority",
                entity_type="scheduled_job",
                entity_id=job.job_id,
                payload=payload,
            )

    def _audit_failure(self, job_id: str, action_id: str, exc: Exception) -> None:
        if self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_failed",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload={"action_id": action_id, "error_type": type(exc).__name__},
            )

    def _required_job(self, job_id: str) -> ScheduledJob:
        job_id = _require_job_id(job_id)
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown scheduled job: {job_id}")
        return job

    def _audit_change(self, event_type: str, job: ScheduledJob) -> None:
        if self._audit is not None:
            self._audit.append(
                event_type=event_type,
                entity_type="scheduled_job",
                entity_id=job.job_id,
                payload={
                    "action_id": job.action_id,
                    "trigger_kind": job.trigger_kind.value,
                    "enabled": job.enabled,
                },
            )


def _require_job_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("job_id must be an exact string")
    if not value or value != value.strip():
        raise ValueError("job_id must be non-empty and whitespace-stable")
    return value


def _make_trigger(job: ScheduledJob) -> object:
    params = dict(job.trigger)
    if job.trigger_kind is TriggerKind.DATE:
        return DateTrigger(**params)
    if job.trigger_kind is TriggerKind.INTERVAL:
        return IntervalTrigger(**params)
    if job.trigger_kind is TriggerKind.CRON:
        return CronTrigger(**params)
    raise ValueError(f"unsupported trigger kind: {job.trigger_kind}")