from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from nika_core.kernel.audit import AuditLog
from nika_core.scheduler.contracts import ScheduledJob, SchedulerPort, TriggerKind
from nika_core.scheduler.store import ScheduledJobStore

ActionHandler = Callable[[dict[str, Any]], None]
HandlerResolver = Callable[[str], ActionHandler]


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
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        for job in self._jobs.list_enabled():
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
            if job.enabled:
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
        if self._started:
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
        # A cancellation can win after APScheduler has queued this callback but before the
        # callback starts. In that race there is no durable job left to execute, so cancelling
        # remains authoritative instead of surfacing a spurious KeyError.
        job = self._jobs.get(job_id)
        if job is None or not job.enabled:
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

        # DATE jobs are durable one-shot intents. APScheduler removes its in-memory DateTrigger
        # after it fires, but ScheduledJobStore is Nika's restart authority. Retire that durable
        # row only after successful execution so a later restart cannot replay a completed wait.
        # Failed executions intentionally retain the row and remain subject to the existing
        # explicit misfire_grace_seconds policy on recovery.
        if job.trigger_kind is TriggerKind.DATE:
            self._jobs.delete(job_id)

        if self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_completed",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload={"action_id": job.action_id},
            )

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        # A DATE intent that is beyond its configured misfire grace is terminal. Keeping the
        # durable row would reinstall the already-expired wait on every future process start.
        job = self._jobs.get(event.job_id)
        if job is None or job.trigger_kind is not TriggerKind.DATE:
            return
        if not self._jobs.delete(event.job_id):
            return
        if self._audit is not None:
            self._audit.append(
                event_type="scheduler.job_missed",
                entity_type="scheduled_job",
                entity_id=event.job_id,
                payload={
                    "action_id": job.action_id,
                    "misfire_grace_seconds": job.misfire_grace_seconds,
                },
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
        run_date = params.get("run_date")
        if run_date is None:
            raise ValueError("DATE trigger requires run_date")
        if isinstance(run_date, str):
            normalized = run_date.replace("Z", "+00:00")
            try:
                run_date = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError("DATE run_date must be an ISO-8601 datetime") from exc
        if not isinstance(run_date, datetime):
            raise ValueError("DATE run_date must be a datetime or ISO-8601 string")
        if run_date.tzinfo is None or run_date.utcoffset() is None:
            raise ValueError("DATE run_date must be timezone-aware")
        params["run_date"] = run_date.astimezone(UTC)
        return DateTrigger(**params)
    if job.trigger_kind is TriggerKind.INTERVAL:
        return IntervalTrigger(**params)
    if job.trigger_kind is TriggerKind.CRON:
        return CronTrigger(**params)
    raise ValueError(f"unsupported trigger kind: {job.trigger_kind}")
