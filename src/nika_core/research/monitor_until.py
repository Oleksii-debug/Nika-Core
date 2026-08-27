from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.scheduler import ScheduledJob, ScheduledJobStore, SchedulerPort, TriggerKind

Clock = Callable[[], datetime]


class MonitorConditionState(StrEnum):
    PENDING = "pending"
    NOT_MET = "not_met"
    MATCHED = "matched"


class MonitorStopReason(StrEnum):
    CONDITION_MET = "condition_met"
    DEADLINE_REACHED = "deadline_reached"


class MonitorRunState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class MonitorUntilStatus:
    schedule_id: str
    deadline_at: datetime
    condition_state: MonitorConditionState
    stop_reason: MonitorStopReason | None
    stopped_at: datetime | None
    last_observed_at: datetime | None
    enabled: bool

    @property
    def run_state(self) -> MonitorRunState:
        if self.stop_reason is not None:
            return MonitorRunState.STOPPED
        if self.enabled:
            return MonitorRunState.ACTIVE
        return MonitorRunState.PAUSED


class MonitorUntilConditionService:
    """Durable terminal policy for a recurring monitor.

    SchedulerPort owns wake-ups. This service owns only the Nika-specific
    "until deadline OR condition" lifecycle. The recurring ScheduledJob is the
    authoritative durable monitor record; the deadline DATE job is a derived
    wake-up hint and never overrides the canonical deadline in that record.
    """

    META_KEY = "_nika_monitor_until"
    META_VERSION = 1
    DEADLINE_ACTION_ID = "research.monitor_until.deadline"
    DEADLINE_JOB_SUFFIX = "::deadline"

    def __init__(
        self,
        *,
        scheduler: SchedulerPort,
        jobs: ScheduledJobStore,
        clock: Clock | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._jobs = jobs
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, job: ScheduledJob, *, deadline_at: datetime) -> MonitorUntilStatus:
        if job.trigger_kind is TriggerKind.DATE:
            raise ValueError("monitor-until requires a recurring INTERVAL or CRON job")
        if "end_date" in job.trigger:
            raise ValueError(
                "monitor deadline must be canonical monitor state, not trigger end_date"
            )
        if job.max_instances != 1:
            raise ValueError("monitor-until requires max_instances=1")
        deadline = _as_utc(deadline_at, "deadline_at")

        existing = self._jobs.get(job.job_id)
        if existing is not None:
            status = self._status_from_job(existing)
            self._assert_same_registration(existing, job, deadline)
            if status.stop_reason is not None:
                self._remove_deadline_guard(job.job_id)
                return status
            return self.reconcile(job.job_id)

        if self.META_KEY in job.payload:
            raise ValueError(f"payload key {self.META_KEY!r} is reserved")
        now = self._now()
        terminal = now >= deadline
        metadata = {
            "version": self.META_VERSION,
            "deadline_at": deadline.isoformat(),
            "condition_state": MonitorConditionState.PENDING.value,
            "stop_reason": (
                MonitorStopReason.DEADLINE_REACHED.value if terminal else None
            ),
            "stopped_at": deadline.isoformat() if terminal else None,
            "last_observed_at": None,
        }
        persisted = self._with_metadata(job, metadata, enabled=(job.enabled and not terminal))
        self._scheduler.upsert(persisted)
        if terminal:
            self._remove_deadline_guard(job.job_id)
        else:
            self._ensure_deadline_guard(job.job_id, deadline)
        return self._status_from_job(self._required_job(job.job_id))

    def status(self, schedule_id: str) -> MonitorUntilStatus:
        return self._status_from_job(self._required_job(schedule_id))

    def reconcile(self, schedule_id: str) -> MonitorUntilStatus:
        job = self._required_job(schedule_id)
        status = self._status_from_job(job)
        if status.stop_reason is not None:
            if job.enabled:
                self._scheduler.upsert(
                    self._with_metadata(job, self._metadata(job), enabled=False)
                )
            self._remove_deadline_guard(schedule_id)
            return self._status_from_job(self._required_job(schedule_id))

        now = self._now()
        if now >= status.deadline_at:
            return self._stop(
                job,
                reason=MonitorStopReason.DEADLINE_REACHED,
                condition_state=status.condition_state,
                stopped_at=status.deadline_at,
                last_observed_at=status.last_observed_at,
            )
        self._ensure_deadline_guard(schedule_id, status.deadline_at)
        return status

    def before_check(self, schedule_id: str) -> bool:
        """Return True only when a scheduled monitor may perform its next check."""
        job = self._required_job(schedule_id)
        status = self._status_from_job(job)
        if status.stop_reason is not None:
            return False
        if self._now() >= status.deadline_at:
            self._stop(
                job,
                reason=MonitorStopReason.DEADLINE_REACHED,
                condition_state=status.condition_state,
                stopped_at=status.deadline_at,
                last_observed_at=status.last_observed_at,
            )
            return False
        return job.enabled

    def record_condition(
        self,
        schedule_id: str,
        *,
        matched: bool,
        observed_at: datetime,
    ) -> MonitorUntilStatus:
        observed = _as_utc(observed_at, "observed_at")
        job = self._required_job(schedule_id)
        status = self._status_from_job(job)
        if status.stop_reason is not None:
            return status
        if status.last_observed_at is not None and observed < status.last_observed_at:
            raise ValueError("condition observation is older than canonical monitor state")

        condition_state = (
            MonitorConditionState.MATCHED if matched else MonitorConditionState.NOT_MET
        )
        if observed >= status.deadline_at:
            return self._stop(
                job,
                reason=MonitorStopReason.DEADLINE_REACHED,
                condition_state=condition_state,
                stopped_at=status.deadline_at,
                last_observed_at=observed,
            )
        if matched:
            return self._stop(
                job,
                reason=MonitorStopReason.CONDITION_MET,
                condition_state=MonitorConditionState.MATCHED,
                stopped_at=observed,
                last_observed_at=observed,
            )

        metadata = self._metadata(job)
        metadata.update(
            {
                "condition_state": MonitorConditionState.NOT_MET.value,
                "last_observed_at": observed.isoformat(),
            }
        )
        self._scheduler.upsert(self._with_metadata(job, metadata, enabled=job.enabled))
        return self._status_from_job(self._required_job(schedule_id))

    def pause(self, schedule_id: str) -> MonitorUntilStatus:
        status = self.status(schedule_id)
        if status.stop_reason is not None:
            return status
        self._scheduler.pause(schedule_id)
        return self.status(schedule_id)

    def resume(self, schedule_id: str) -> MonitorUntilStatus:
        status = self.status(schedule_id)
        if status.stop_reason is not None:
            return status
        if self._now() >= status.deadline_at:
            return self._stop(
                self._required_job(schedule_id),
                reason=MonitorStopReason.DEADLINE_REACHED,
                condition_state=status.condition_state,
                stopped_at=status.deadline_at,
                last_observed_at=status.last_observed_at,
            )
        self._scheduler.resume(schedule_id)
        self._ensure_deadline_guard(schedule_id, status.deadline_at)
        return self.status(schedule_id)

    def deadline_action_handler(self, payload: dict[str, object]) -> None:
        schedule_id = payload.get("schedule_id")
        if not isinstance(schedule_id, str) or not schedule_id.strip():
            raise ValueError("schedule_id is required")
        self.reconcile(schedule_id.strip())

    @staticmethod
    def render_status_text(status: MonitorUntilStatus) -> str:
        lines = [
            "Monitoring status",
            f"Schedule: {status.schedule_id}",
            f"State: {status.run_state.value}",
            f"Condition: {status.condition_state.value}",
            f"Deadline: {status.deadline_at.isoformat()}",
        ]
        if status.stop_reason is MonitorStopReason.CONDITION_MET:
            lines.extend(
                (
                    "Stopped because: condition matched before the deadline.",
                    f"Stopped at: {status.stopped_at.isoformat()}",
                )
            )
        elif status.stop_reason is MonitorStopReason.DEADLINE_REACHED:
            boundary_note = (
                " Condition observations at the exact deadline are resolved as deadline."
                if status.condition_state is MonitorConditionState.MATCHED
                else ""
            )
            lines.extend(
                (
                    f"Stopped because: deadline reached.{boundary_note}",
                    f"Stopped at: {status.stopped_at.isoformat()}",
                )
            )
        else:
            lines.append("Stopped because: not stopped.")
        return "\n".join(lines) + "\n"

    def _stop(
        self,
        job: ScheduledJob,
        *,
        reason: MonitorStopReason,
        condition_state: MonitorConditionState,
        stopped_at: datetime,
        last_observed_at: datetime | None,
    ) -> MonitorUntilStatus:
        current = self._status_from_job(job)
        if current.stop_reason is not None:
            self._remove_deadline_guard(job.job_id)
            return current
        metadata = self._metadata(job)
        metadata.update(
            {
                "condition_state": condition_state.value,
                "stop_reason": reason.value,
                "stopped_at": _as_utc(stopped_at, "stopped_at").isoformat(),
                "last_observed_at": (
                    _as_utc(last_observed_at, "last_observed_at").isoformat()
                    if last_observed_at is not None
                    else None
                ),
            }
        )
        # Persist enabled=False before removing the derived guard. APSchedulerAdapter
        # dispatch re-reads the durable job and therefore fails closed if a runtime
        # wake-up races this terminalization.
        self._scheduler.upsert(self._with_metadata(job, metadata, enabled=False))
        self._remove_deadline_guard(job.job_id)
        return self._status_from_job(self._required_job(job.job_id))

    def _ensure_deadline_guard(self, schedule_id: str, deadline: datetime) -> None:
        guard_id = self._deadline_job_id(schedule_id)
        existing = self._jobs.get(guard_id)
        if existing is not None and (
            existing.action_id != self.DEADLINE_ACTION_ID
            or existing.payload != {"schedule_id": schedule_id}
        ):
            raise ValueError(f"deadline guard job id collision: {guard_id}")
        guard = ScheduledJob(
            job_id=guard_id,
            action_id=self.DEADLINE_ACTION_ID,
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": deadline.isoformat()},
            payload={"schedule_id": schedule_id},
            enabled=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_seconds=None,
        )
        self._scheduler.upsert(guard)

    def _remove_deadline_guard(self, schedule_id: str) -> None:
        self._scheduler.remove(self._deadline_job_id(schedule_id))

    def _assert_same_registration(
        self,
        existing: ScheduledJob,
        requested: ScheduledJob,
        deadline: datetime,
    ) -> None:
        status = self._status_from_job(existing)
        if status.deadline_at != deadline:
            raise ValueError("existing monitor has a different canonical deadline")
        existing_payload = dict(existing.payload)
        existing_payload.pop(self.META_KEY, None)
        requested_payload = dict(requested.payload)
        requested_payload.pop(self.META_KEY, None)
        if (
            existing.action_id != requested.action_id
            or existing.trigger_kind is not requested.trigger_kind
            or existing.trigger != requested.trigger
            or existing_payload != requested_payload
            or existing.coalesce != requested.coalesce
            or existing.max_instances != requested.max_instances
            or existing.misfire_grace_seconds != requested.misfire_grace_seconds
        ):
            raise ValueError("existing monitor registration differs from requested configuration")

    def _status_from_job(self, job: ScheduledJob) -> MonitorUntilStatus:
        metadata = self._metadata(job)
        deadline = _parse_datetime(metadata.get("deadline_at"), "deadline_at")
        condition_raw = metadata.get("condition_state")
        try:
            condition = MonitorConditionState(condition_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid monitor condition_state") from exc
        stop_raw = metadata.get("stop_reason")
        try:
            reason = None if stop_raw is None else MonitorStopReason(stop_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid monitor stop_reason") from exc
        stopped_at = _parse_optional_datetime(metadata.get("stopped_at"), "stopped_at")
        last_observed = _parse_optional_datetime(
            metadata.get("last_observed_at"), "last_observed_at"
        )
        if (reason is None) != (stopped_at is None):
            raise ValueError("monitor stop_reason and stopped_at must be set together")
        if reason is MonitorStopReason.CONDITION_MET:
            if condition is not MonitorConditionState.MATCHED:
                raise ValueError("condition terminal monitor must have matched condition state")
            if stopped_at is None or stopped_at >= deadline:
                raise ValueError("condition terminal observation must be before deadline")
        if reason is MonitorStopReason.DEADLINE_REACHED and stopped_at != deadline:
            raise ValueError("deadline terminal monitor must stop at canonical deadline")
        return MonitorUntilStatus(
            schedule_id=job.job_id,
            deadline_at=deadline,
            condition_state=condition,
            stop_reason=reason,
            stopped_at=stopped_at,
            last_observed_at=last_observed,
            enabled=job.enabled,
        )

    def _metadata(self, job: ScheduledJob) -> dict[str, object]:
        raw = job.payload.get(self.META_KEY)
        if not isinstance(raw, dict):
            raise ValueError(f"scheduled job {job.job_id!r} is not a monitor-until job")
        metadata = dict(raw)
        if metadata.get("version") != self.META_VERSION:
            raise ValueError("unsupported monitor-until metadata version")
        return metadata

    def _with_metadata(
        self,
        job: ScheduledJob,
        metadata: dict[str, object],
        *,
        enabled: bool,
    ) -> ScheduledJob:
        payload = dict(job.payload)
        payload[self.META_KEY] = dict(metadata)
        return ScheduledJob(
            job_id=job.job_id,
            action_id=job.action_id,
            trigger_kind=job.trigger_kind,
            trigger=dict(job.trigger),
            payload=payload,
            enabled=enabled,
            coalesce=job.coalesce,
            max_instances=job.max_instances,
            misfire_grace_seconds=job.misfire_grace_seconds,
        )

    def _required_job(self, schedule_id: str) -> ScheduledJob:
        job = self._jobs.get(schedule_id)
        if job is None:
            raise KeyError(f"unknown monitor schedule: {schedule_id}")
        return job

    def _now(self) -> datetime:
        return _as_utc(self._clock(), "clock")

    @classmethod
    def _deadline_job_id(cls, schedule_id: str) -> str:
        return f"{schedule_id}{cls.DEADLINE_JOB_SUFFIX}"


def _as_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"monitor {label} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"monitor {label} must be an ISO-8601 datetime") from exc
    return _as_utc(parsed, label)


def _parse_optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, label)
