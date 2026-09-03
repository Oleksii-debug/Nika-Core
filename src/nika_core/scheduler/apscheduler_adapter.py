from __future__ import annotations

from collections.abc import Callable
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

    def start(self) -> None:
        if self._started:
            return
        for job in self._jobs.list_enabled():
            if self._task_authority_allows(job):
                self._install(job)
        self._scheduler.start()
        self._started = True

    def shutdown(self, *, wait: bool = True) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=wait)
        self._started = False

    def upsert(self, job: ScheduledJob) -> None:
        self._jobs.upsert(job)
        if self._started:
            if job.enabled and self._task_authority_allows(job):
                self._install(job)
            elif self._scheduler.get_job(job.job_id) is not None:
                self._scheduler.remove_job(job.job_id)
        self._audit_change("scheduler.job_upserted", job)

    def remove(self, job_id: str) -> bool:
        removed = self._jobs.delete(job_id)
        if self._started and self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)
        if removed and self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_removed",
                entity_type="scheduled_job",
                entity_id=job_id,
            )
        return removed

    def pause(self, job_id: str) -> None:
        job = self._required_job(job_id)
        self._jobs.set_enabled(job_id, False)
        if self._started and self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)
        self._audit_change("scheduler.job_paused", job)

    def resume(self, job_id: str) -> None:
        job = self._required_job(job_id)
        self._jobs.set_enabled(job_id, True)
        enabled_job = ScheduledJob(
            job_id=job.job_id,
            action_id=job.action_id,
            trigger_kind=job.trigger_kind,
            trigger=job.trigger,
            payload=job.payload,
            enabled=True,
            coalesce=job.coalesce,
            max_instances=job.max_instances,
            misfire_grace_seconds=job.misfire_grace_seconds,
        )
        if self._started and self._task_authority_allows(enabled_job):
            self._install(enabled_job)
        self._audit_change("scheduler.job_resumed", enabled_job)

    def has_runtime_job(self, job_id: str) -> bool:
        return self._scheduler.get_job(job_id) is not None

    def _install(self, job: ScheduledJob) -> None:
        self._scheduler.add_job(
            self._dispatch,
            trigger=_make_trigger(job),
            id=job.job_id,
            args=(job.job_id,),
            replace_existing=True,
            coalesce=job.coalesce,
            max_instances=job.max_instances,
            misfire_grace_time=job.misfire_grace_seconds,
        )

    def _dispatch(self, job_id: str) -> None:
        job = self._required_job(job_id)
        if not job.enabled or not self._task_authority_allows(job):
            return
        if self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_started",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload={"action_id": job.action_id},
            )
        handler = self._handler_resolver(job.action_id)
        try:
            handler(dict(job.payload))
        except Exception as exc:
            if self._audit is not None:
                self._audit.append(
                    event_type="scheduler.job_failed",
                    entity_type="scheduled_job",
                    entity_id=job_id,
                    payload={"action_id": job.action_id, "error_type": type(exc).__name__},
                )
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
        self._jobs.set_enabled(job.job_id, False)
        if self._started and self._scheduler.get_job(job.job_id) is not None:
            self._scheduler.remove_job(job.job_id)
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

    def _required_job(self, job_id: str) -> ScheduledJob:
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


def _make_trigger(job: ScheduledJob) -> object:
    params = dict(job.trigger)
    if job.trigger_kind is TriggerKind.DATE:
        return DateTrigger(**params)
    if job.trigger_kind is TriggerKind.INTERVAL:
        return IntervalTrigger(**params)
    if job.trigger_kind is TriggerKind.CRON:
        return CronTrigger(**params)
    raise ValueError(f"unsupported trigger kind: {job.trigger_kind}")
