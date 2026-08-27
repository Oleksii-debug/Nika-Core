from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import object as _object

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import Checkpoint, CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.research.models import RefreshDisposition, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.web_service import HttpResearchService
from nika_core.scheduler.contracts import ScheduledJob, SchedulerPort, TriggerKind

_MONITOR_KIND = "v01_monitoring_loop"
_MONITOR_AGENT_ID = "research.v01_monitor"
_CHECKPOINT_STAGE = "v01_monitor_observation"
_DEFAULT_ACTION_ID = "research.v01_monitor_tick"
_SUPPORTED_REFRESHES = frozenset(
    {
        RefreshDisposition.CHANGED,
        RefreshDisposition.NOT_MODIFIED,
        RefreshDisposition.UNCHANGED,
    }
)


class MonitorCondition(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class MonitorRunDisposition(StrEnum):
    OBSERVED = "observed"
    DUPLICATE = "duplicate"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    DEADLINE_REACHED = "deadline_reached"
    INACTIVE = "inactive"


class MonitoringFetchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    source_id: str
    workspace_id: str
    declared_locator: str
    resolved_locator: str
    snapshot_id: str
    document_id: str
    normalized_sha256: str
    source_observed_at: str


@dataclass(frozen=True, slots=True)
class MonitorRunResult:
    disposition: MonitorRunDisposition
    occurrence_id: str | None
    observation: NormalizedObservation | None
    previous_normalized_sha256: str | None
    changed: bool | None
    condition_met: bool
    refresh_disposition: RefreshDisposition | None


@dataclass(frozen=True, slots=True)
class MonitorHandle:
    task_id: str
    job_id: str
    next_check_at: datetime


@dataclass(frozen=True, slots=True)
class MonitorStartResult:
    handle: MonitorHandle
    initial: MonitorRunResult


@dataclass(frozen=True, slots=True)
class _MonitorConfig:
    task_id: str
    workspace_id: str
    source_id: str
    locator: str
    interval_seconds: int
    condition: MonitorCondition
    anchor_at: datetime
    deadline_at: datetime | None


class V01MonitoringLoop:
    """Thin V0.1 monitor over existing Research fetch, checkpoints and SchedulerPort."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        network_repository: NetworkResearchRepository,
        web: HttpResearchService,
        tasks: TaskQueue,
        checkpoints: CheckpointService,
        scheduler: SchedulerPort,
        action_id: str = _DEFAULT_ACTION_ID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not action_id.strip():
            raise ValueError("action_id must not be empty")
        self._store = store
        self._network = network_repository
        self._web = web
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._scheduler = scheduler
        self._action_id = action_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def start(
        self,
        source: SourceSpec,
        *,
        interval_seconds: int,
        condition: MonitorCondition = MonitorCondition.CHANGED,
        deadline_at: datetime | None = None,
    ) -> MonitorStartResult:
        if source.kind is not SourceKind.HTTP:
            raise ValueError("V0.1 monitoring supports declared HTTP sources only")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        now = _as_utc(self._clock(), field="clock")
        deadline = _optional_utc(deadline_at, field="deadline_at")
        if deadline is not None and deadline <= now:
            raise ValueError("deadline_at must be in the future")

        self._ensure_declared_source(source, allow_register=True)
        payload: dict[str, object] = {
            "kind": _MONITOR_KIND,
            "source_id": source.source_id,
            "workspace_id": source.workspace_id,
            "locator": source.locator,
            "interval_seconds": interval_seconds,
            "condition": condition.value,
            "anchor_at": now.isoformat(),
            "deadline_at": deadline.isoformat() if deadline is not None else None,
        }
        task = self._tasks.create(
            workspace_id=source.workspace_id,
            agent_id=_MONITOR_AGENT_ID,
            payload=payload,
        )
        self._tasks.transition(task.task_id, TaskState.READY)
        config = self._load_config(task.task_id)
        self._scheduler.upsert(self._scheduled_job(config))
        initial = self.run(task.task_id, occurrence_at=now)
        return MonitorStartResult(
            handle=MonitorHandle(
                task_id=task.task_id,
                job_id=self.job_id(task.task_id),
                next_check_at=now + timedelta(seconds=interval_seconds),
            ),
            initial=initial,
        )

    def run(
        self,
        task_id: str,
        *,
        occurrence_at: datetime | None = None,
    ) -> MonitorRunResult:
        task = self._tasks.get(task_id)
        if task.state is TaskState.PAUSED:
            return self._inactive_result(MonitorRunDisposition.PAUSED)
        if task.state is TaskState.CANCELLED:
            return self._inactive_result(MonitorRunDisposition.CANCELLED)
        if task.state not in {TaskState.READY, TaskState.RUNNING}:
            return self._inactive_result(MonitorRunDisposition.INACTIVE)

        config = self._load_config(task_id)
        now = _as_utc(occurrence_at or self._clock(), field="occurrence_at")
        if now < config.anchor_at:
            raise ValueError("occurrence_at must not precede monitor anchor")
        sequence = int((now - config.anchor_at).total_seconds() // config.interval_seconds)
        occurrence_id = f"{task_id}:{sequence}"
        if config.deadline_at is not None and now >= config.deadline_at:
            return MonitorRunResult(
                disposition=MonitorRunDisposition.DEADLINE_REACHED,
                occurrence_id=occurrence_id,
                observation=None,
                previous_normalized_sha256=None,
                changed=None,
                condition_met=False,
                refresh_disposition=None,
            )

        self._ensure_declared_source(
            SourceSpec(config.source_id, config.workspace_id, SourceKind.HTTP, config.locator),
            allow_register=False,
        )
        latest = self._checkpoints.latest(task_id)
        if latest is not None and latest.stage == _CHECKPOINT_STAGE:
            previous_sequence = _required_int(latest.payload, "occurrence_sequence")
            if previous_sequence == sequence:
                return self._result_from_checkpoint(latest, MonitorRunDisposition.DUPLICATE)
            if previous_sequence > sequence:
                raise ValueError("occurrence sequence would move backwards")

        previous = self._previous_observation(latest)
        if previous is None:
            previous = self._current_observation(config)

        if task.state is TaskState.READY:
            self._tasks.transition(task_id, TaskState.RUNNING)

        refresh = self._web.refresh_source(config.source_id, task_id=task_id)
        if refresh.disposition not in _SUPPORTED_REFRESHES:
            detail = refresh.error_code or refresh.message or refresh.disposition.value
            raise MonitoringFetchError(
                f"V0.1 monitor cannot normalize source {config.source_id}: {detail}"
            )
        current = self._current_observation(config)
        if current is None:
            raise MonitoringFetchError(
                f"V0.1 monitor has no normalized snapshot for source {config.source_id}"
            )

        previous_hash = previous.normalized_sha256 if previous is not None else None
        changed = (
            None if previous_hash is None else previous_hash != current.normalized_sha256
        )
        condition_met = changed is not None and (
            (config.condition is MonitorCondition.CHANGED and changed)
            or (config.condition is MonitorCondition.UNCHANGED and not changed)
        )
        checkpoint = self._checkpoints.save(
            task_id=task_id,
            stage=_CHECKPOINT_STAGE,
            payload={
                "occurrence_id": occurrence_id,
                "occurrence_sequence": sequence,
                "observed_at": now.isoformat(),
                "source_id": current.source_id,
                "workspace_id": current.workspace_id,
                "declared_locator": current.declared_locator,
                "resolved_locator": current.resolved_locator,
                "snapshot_id": current.snapshot_id,
                "document_id": current.document_id,
                "normalized_sha256": current.normalized_sha256,
                "source_observed_at": current.source_observed_at,
                "previous_normalized_sha256": previous_hash,
                "changed": changed,
                "condition": config.condition.value,
                "condition_met": condition_met,
                "refresh_disposition": refresh.disposition.value,
            },
        )
        return self._result_from_checkpoint(checkpoint, MonitorRunDisposition.OBSERVED)

    def handle_scheduled(self, payload: dict[str, object]) -> None:
        task_id = payload.get("monitor_task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("scheduled monitoring payload requires monitor_task_id")
        self.run(task_id)

    def pause(self, task_id: str) -> TaskState:
        task = self._tasks.get(task_id)
        if task.state is TaskState.PAUSED:
            return task.state
        if task.state not in {TaskState.READY, TaskState.RUNNING}:
            raise ValueError(f"monitor cannot pause from {task.state.value}")
        self._tasks.transition(task_id, TaskState.PAUSED)
        self._scheduler.pause(self.job_id(task_id))
        return TaskState.PAUSED

    def resume(self, task_id: str) -> TaskState:
        task = self._tasks.get(task_id)
        if task.state in {TaskState.READY, TaskState.RUNNING}:
            return task.state
        if task.state is not TaskState.PAUSED:
            raise ValueError(f"monitor cannot resume from {task.state.value}")
        config = self._load_config(task_id)
        now = _as_utc(self._clock(), field="clock")
        if config.deadline_at is not None and now >= config.deadline_at:
            raise ValueError("monitor deadline has already been reached")
        self._scheduler.resume(self.job_id(task_id))
        self._tasks.transition(task_id, TaskState.READY)
        return TaskState.READY

    def cancel(self, task_id: str) -> TaskState:
        task = self._tasks.get(task_id)
        if task.state is not TaskState.CANCELLED:
            if task.state not in {
                TaskState.CREATED,
                TaskState.READY,
                TaskState.RUNNING,
                TaskState.PAUSED,
            }:
                raise ValueError(f"monitor cannot cancel from {task.state.value}")
            self._tasks.transition(task_id, TaskState.CANCELLED)
        self._scheduler.remove(self.job_id(task_id))
        return TaskState.CANCELLED

    @staticmethod
    def job_id(task_id: str) -> str:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        return f"v01-monitor:{task_id}"

    def _scheduled_job(self, config: _MonitorConfig) -> ScheduledJob:
        trigger: dict[str, object] = {
            "seconds": config.interval_seconds,
            "start_date": (
                config.anchor_at + timedelta(seconds=config.interval_seconds)
            ).isoformat(),
        }
        if config.deadline_at is not None:
            trigger["end_date"] = config.deadline_at.isoformat()
        return ScheduledJob(
            job_id=self.job_id(config.task_id),
            action_id=self._action_id,
            trigger_kind=TriggerKind.INTERVAL,
            trigger=trigger,
            payload={"monitor_task_id": config.task_id},
            enabled=True,
            coalesce=True,
            max_instances=1,
        )

    def _load_config(self, task_id: str) -> _MonitorConfig:
        task = self._tasks.get(task_id)
        payload = task.payload
        if task.agent_id != _MONITOR_AGENT_ID or payload.get("kind") != _MONITOR_KIND:
            raise ValueError(f"task {task_id} is not a V0.1 monitoring task")
        workspace_id = _required_str(payload, "workspace_id")
        if task.workspace_id != workspace_id:
            raise ValueError("monitor task workspace identity mismatch")
        return _MonitorConfig(
            task_id=task_id,
            workspace_id=workspace_id,
            source_id=_required_str(payload, "source_id"),
            locator=_required_str(payload, "locator"),
            interval_seconds=_required_int(payload, "interval_seconds"),
            condition=MonitorCondition(_required_str(payload, "condition")),
            anchor_at=_parse_utc(_required_str(payload, "anchor_at"), field="anchor_at"),
            deadline_at=_parse_optional_utc(payload.get("deadline_at"), field="deadline_at"),
        )

    def _ensure_declared_source(self, source: SourceSpec, *, allow_register: bool) -> None:
        try:
            current = self._network.get_source(source.source_id)
        except KeyError:
            if not allow_register:
                raise ValueError(f"declared HTTP source disappeared: {source.source_id}") from None
            self._web.register_source(source)
            current = self._network.get_source(source.source_id)
        if current.workspace_id != source.workspace_id or current.url != source.locator:
            raise ValueError(
                "declared HTTP source identity does not match the persisted source; refusing mutation"
            )

    def _current_observation(self, config: _MonitorConfig) -> NormalizedObservation | None:
        with self._store.connection() as conn:
            row = conn.execute(
                """SELECT h.source_id, h.workspace_id, h.url, h.final_url,
                           s.snapshot_id, s.document_id, s.observed_at,
                           d.normalized_sha256
                    FROM research_http_sources AS h
                    JOIN research_http_snapshots AS s
                      ON s.source_id = h.source_id
                     AND s.raw_sha256 = h.current_raw_sha256
                    JOIN corpus_documents AS d ON d.document_id = s.document_id
                    WHERE h.source_id = ? AND h.workspace_id = ?""",
                (config.source_id, config.workspace_id),
            ).fetchone()
        if row is None:
            return None
        if row["url"] != config.locator:
            raise ValueError("persisted source locator changed during monitoring")
        return NormalizedObservation(
            source_id=row["source_id"],
            workspace_id=row["workspace_id"],
            declared_locator=row["url"],
            resolved_locator=row["final_url"] or row["url"],
            snapshot_id=row["snapshot_id"],
            document_id=row["document_id"],
            normalized_sha256=row["normalized_sha256"],
            source_observed_at=row["observed_at"],
        )

    @staticmethod
    def _previous_observation(checkpoint: Checkpoint | None) -> NormalizedObservation | None:
        if checkpoint is None or checkpoint.stage != _CHECKPOINT_STAGE:
            return None
        return _observation_from_payload(checkpoint.payload)

    @staticmethod
    def _result_from_checkpoint(
        checkpoint: Checkpoint,
        disposition: MonitorRunDisposition,
    ) -> MonitorRunResult:
        payload = checkpoint.payload
        changed = payload.get("changed")
        if changed is not None and not isinstance(changed, bool):
            raise ValueError("monitor checkpoint changed flag is invalid")
        return MonitorRunResult(
            disposition=disposition,
            occurrence_id=_required_str(payload, "occurrence_id"),
            observation=_observation_from_payload(payload),
            previous_normalized_sha256=_optional_str(
                payload.get("previous_normalized_sha256"),
                "previous_normalized_sha256",
            ),
            changed=changed,
            condition_met=_required_bool(payload, "condition_met"),
            refresh_disposition=RefreshDisposition(
                _required_str(payload, "refresh_disposition")
            ),
        )

    @staticmethod
    def _inactive_result(disposition: MonitorRunDisposition) -> MonitorRunResult:
        return MonitorRunResult(
            disposition=disposition,
            occurrence_id=None,
            observation=None,
            previous_normalized_sha256=None,
            changed=None,
            condition_met=False,
            refresh_disposition=None,
        )


def _observation_from_payload(payload: dict[str, object]) -> NormalizedObservation:
    return NormalizedObservation(
        source_id=_required_str(payload, "source_id"),
        workspace_id=_required_str(payload, "workspace_id"),
        declared_locator=_required_str(payload, "declared_locator"),
        resolved_locator=_required_str(payload, "resolved_locator"),
        snapshot_id=_required_str(payload, "snapshot_id"),
        document_id=_required_str(payload, "document_id"),
        normalized_sha256=_required_str(payload, "normalized_sha256"),
        source_observed_at=_required_str(payload, "source_observed_at"),
    )


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"monitor payload requires non-empty {key}")
    return value


def _optional_str(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"monitor payload {key} must be a string or null")
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"monitor payload {key} must be a positive integer")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"monitor payload {key} must be boolean")
    return value


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None, *, field: str) -> datetime | None:
    return None if value is None else _as_utc(value, field=field)


def _parse_utc(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    return _as_utc(parsed, field=field)


def _parse_optional_utc(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 datetime or null")
    return _parse_utc(value, field=field)
