from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import Checkpoint, CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.research.models import RefreshDisposition, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.web_service import HttpResearchService
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceDecision,
    RecurrenceInvocation,
    RecurrenceState,
    RecurrenceStatus,
)

_MONITOR_KIND = "v01_monitoring_loop"
_MONITOR_AGENT_ID = "research.v01_monitor"
_CHECKPOINT_STAGE = "v01_monitor_observation"
_DEFAULT_ACTION_ID = "research.v01_monitor_occurrence"
_RECURRENCE_PREFIX = "v01-monitor"
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
    occurrence_id: str
    scheduled_for: datetime
    observation: NormalizedObservation | None
    previous_normalized_sha256: str | None
    changed: bool | None
    condition_met: bool
    refresh_disposition: RefreshDisposition | None


@dataclass(frozen=True, slots=True)
class MonitorHandle:
    task_id: str
    recurrence_id: str
    next_check_at: datetime | None


@dataclass(frozen=True, slots=True)
class _MonitorConfig:
    task_id: str
    workspace_id: str
    source_id: str
    locator: str
    condition: MonitorCondition


class V01MonitoringLoop:
    """Research occurrence wiring over the canonical durable recurrence service.

    Scheduling, occurrence identity, missed-run coalescing, pause/resume/cancel and
    deadline gating remain owned by ``DurableRecurrenceService``. This class owns only
    one research occurrence: fetch/normalize/persist, compare, condition evaluation and
    a durable checkpoint bound to the canonical occurrence id.
    """

    ACTION_ID = _DEFAULT_ACTION_ID

    def __init__(
        self,
        *,
        store: SQLiteStore,
        network_repository: NetworkResearchRepository,
        web: HttpResearchService,
        tasks: TaskQueue,
        checkpoints: CheckpointService,
        recurrence: DurableRecurrenceService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._network = network_repository
        self._web = web
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._recurrence = recurrence
        self._clock = clock or (lambda: datetime.now(UTC))

    def start(
        self,
        source: SourceSpec,
        *,
        interval_seconds: int,
        condition: MonitorCondition = MonitorCondition.CHANGED,
        deadline_at: datetime | None = None,
    ) -> MonitorHandle:
        if source.kind is not SourceKind.HTTP:
            raise ValueError("V0.1 monitoring supports declared HTTP sources only")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be a positive integer")
        now = _as_utc(self._clock(), field="clock")
        deadline = _optional_utc(deadline_at, field="deadline_at")
        if deadline is not None and deadline <= now:
            raise ValueError("deadline_at must be in the future")

        self._ensure_declared_source(source, allow_register=True)
        task = self._tasks.create(
            workspace_id=source.workspace_id,
            agent_id=_MONITOR_AGENT_ID,
            payload={
                "kind": _MONITOR_KIND,
                "source_id": source.source_id,
                "workspace_id": source.workspace_id,
                "locator": source.locator,
                "condition": condition.value,
            },
        )
        self._tasks.transition(task.task_id, TaskState.READY)
        recurrence_id = self.recurrence_id(task.task_id)
        try:
            recurrence = self._recurrence.create(
                recurrence_id=recurrence_id,
                action_id=self.ACTION_ID,
                interval_seconds=interval_seconds,
                start_at=now,
                payload={"monitor_task_id": task.task_id},
                deadline_at=deadline,
            )
        except Exception:  # noqa: BLE001 - fail closed if scheduler persistence fails
            current = self._tasks.get(task.task_id)
            if current.state is TaskState.READY:
                self._tasks.transition(task.task_id, TaskState.CANCELLED)
            raise
        return MonitorHandle(
            task_id=task.task_id,
            recurrence_id=recurrence_id,
            next_check_at=recurrence.next_due_at,
        )

    def handle_occurrence(self, invocation: RecurrenceInvocation) -> RecurrenceDecision:
        task_id = _required_payload_text(invocation.payload, "monitor_task_id")
        expected_recurrence_id = self.recurrence_id(task_id)
        if invocation.recurrence_id != expected_recurrence_id:
            raise ValueError("monitor recurrence identity mismatch")
        recurrence = self._recurrence.get(expected_recurrence_id)
        if recurrence is None:
            raise ValueError("monitor recurrence is missing")
        if (
            recurrence.next_occurrence_id != invocation.occurrence_id
            or recurrence.next_due_at != invocation.scheduled_for
        ):
            raise ValueError("stale or foreign monitor occurrence")

        result = self.run_occurrence(
            task_id,
            occurrence_id=invocation.occurrence_id,
            scheduled_for=invocation.scheduled_for,
        )
        task = self._tasks.get(task_id)
        if task.state is TaskState.CANCELLED:
            self._recurrence.cancel(invocation.recurrence_id)
            return RecurrenceDecision.CONTINUE
        if task.state is TaskState.PAUSED:
            self._recurrence.pause(invocation.recurrence_id)
            return RecurrenceDecision.CONTINUE
        if result.condition_met:
            if task.state is TaskState.RUNNING:
                self._tasks.transition(task_id, TaskState.COMPLETED)
            elif task.state is not TaskState.COMPLETED:
                raise ValueError(
                    f"condition-matched monitor cannot complete from {task.state.value}"
                )
            return RecurrenceDecision.STOP
        return RecurrenceDecision.CONTINUE

    def run_occurrence(
        self,
        task_id: str,
        *,
        occurrence_id: str,
        scheduled_for: datetime,
    ) -> MonitorRunResult:
        occurrence_key = _required_text(occurrence_id, "occurrence_id")
        scheduled = _as_utc(scheduled_for, field="scheduled_for")
        config = self._load_config(task_id)
        latest = self._checkpoints.latest(task_id)
        if latest is not None and latest.stage == _CHECKPOINT_STAGE:
            previous_occurrence_id = _required_payload_text(latest.payload, "occurrence_id")
            previous_scheduled = _parse_utc(
                _required_payload_text(latest.payload, "scheduled_for"),
                field="checkpoint scheduled_for",
            )
            if previous_occurrence_id == occurrence_key:
                if previous_scheduled != scheduled:
                    raise ValueError("monitor occurrence id was replayed with a different schedule")
                return _result_from_checkpoint(latest, MonitorRunDisposition.DUPLICATE)
            if previous_scheduled >= scheduled:
                raise ValueError("stale monitor occurrence cannot move durable evidence backwards")

        task = self._tasks.get(task_id)
        if task.state is TaskState.PAUSED:
            return _inactive_result(MonitorRunDisposition.PAUSED, occurrence_key, scheduled)
        if task.state is TaskState.CANCELLED:
            return _inactive_result(MonitorRunDisposition.CANCELLED, occurrence_key, scheduled)
        if task.state not in {TaskState.READY, TaskState.RUNNING}:
            return _inactive_result(MonitorRunDisposition.INACTIVE, occurrence_key, scheduled)

        self._ensure_declared_source(
            SourceSpec(config.source_id, config.workspace_id, SourceKind.HTTP, config.locator),
            allow_register=False,
        )
        previous = _checkpoint_observation(latest)
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
        changed = None if previous_hash is None else previous_hash != current.normalized_sha256
        condition_met = changed is not None and (
            (config.condition is MonitorCondition.CHANGED and changed)
            or (config.condition is MonitorCondition.UNCHANGED and not changed)
        )
        checkpoint = self._checkpoints.save(
            task_id=task_id,
            stage=_CHECKPOINT_STAGE,
            payload={
                "occurrence_id": occurrence_key,
                "scheduled_for": scheduled.isoformat(),
                "observed_at": _as_utc(self._clock(), field="clock").isoformat(),
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
        return _result_from_checkpoint(checkpoint, MonitorRunDisposition.OBSERVED)

    def pause(self, task_id: str) -> TaskState:
        task = self._tasks.get(task_id)
        if task.state is TaskState.PAUSED:
            self._recurrence.pause(self.recurrence_id(task_id))
            return task.state
        if task.state not in {TaskState.READY, TaskState.RUNNING}:
            raise ValueError(f"monitor cannot pause from {task.state.value}")
        self._tasks.transition(task_id, TaskState.PAUSED)
        self._recurrence.pause(self.recurrence_id(task_id))
        return TaskState.PAUSED

    def resume(self, task_id: str) -> TaskState:
        task = self._tasks.get(task_id)
        if task.state is TaskState.PAUSED:
            self._tasks.transition(task_id, TaskState.READY)
        elif task.state not in {TaskState.READY, TaskState.RUNNING}:
            raise ValueError(f"monitor cannot resume from {task.state.value}")

        recurrence = self._recurrence.get(self.recurrence_id(task_id))
        if recurrence is None:
            raise ValueError("monitor recurrence is missing")
        if recurrence.status is RecurrenceStatus.PAUSED:
            recurrence = self._recurrence.resume(recurrence.recurrence_id)
        if recurrence.status is RecurrenceStatus.COMPLETED:
            return self._complete_task_without_fetch(task_id)
        if recurrence.status is not RecurrenceStatus.ACTIVE:
            raise ValueError(f"monitor recurrence cannot resume from {recurrence.status.value}")
        return self._tasks.get(task_id).state

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
        recurrence = self._recurrence.get(self.recurrence_id(task_id))
        if recurrence is not None:
            self._recurrence.cancel(recurrence.recurrence_id)
        return TaskState.CANCELLED

    def recurrence_state(self, task_id: str) -> RecurrenceState | None:
        return self._recurrence.get(self.recurrence_id(task_id))

    @staticmethod
    def recurrence_id(task_id: str) -> str:
        return f"{_RECURRENCE_PREFIX}:{_required_text(task_id, 'task_id')}"

    def _complete_task_without_fetch(self, task_id: str) -> TaskState:
        task = self._tasks.get(task_id)
        if task.state is TaskState.COMPLETED:
            return task.state
        if task.state is TaskState.PAUSED:
            self._tasks.transition(task_id, TaskState.READY)
            task = self._tasks.get(task_id)
        if task.state is TaskState.READY:
            self._tasks.transition(task_id, TaskState.RUNNING)
            task = self._tasks.get(task_id)
        if task.state is TaskState.RUNNING:
            return self._tasks.transition(task_id, TaskState.COMPLETED)
        return task.state

    def _load_config(self, task_id: str) -> _MonitorConfig:
        task = self._tasks.get(task_id)
        payload = task.payload
        if task.agent_id != _MONITOR_AGENT_ID or payload.get("kind") != _MONITOR_KIND:
            raise ValueError(f"task {task_id} is not a V0.1 monitoring task")
        workspace_id = _required_payload_text(payload, "workspace_id")
        if task.workspace_id != workspace_id:
            raise ValueError("monitor task workspace identity mismatch")
        return _MonitorConfig(
            task_id=task_id,
            workspace_id=workspace_id,
            source_id=_required_payload_text(payload, "source_id"),
            locator=_required_payload_text(payload, "locator"),
            condition=MonitorCondition(_required_payload_text(payload, "condition")),
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
                "declared HTTP source identity does not match the persisted source; "
                "refusing mutation"
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


def _checkpoint_observation(checkpoint: Checkpoint | None) -> NormalizedObservation | None:
    if checkpoint is None or checkpoint.stage != _CHECKPOINT_STAGE:
        return None
    return _observation_from_payload(checkpoint.payload)


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
        occurrence_id=_required_payload_text(payload, "occurrence_id"),
        scheduled_for=_parse_utc(
            _required_payload_text(payload, "scheduled_for"),
            field="checkpoint scheduled_for",
        ),
        observation=_observation_from_payload(payload),
        previous_normalized_sha256=_optional_text(
            payload.get("previous_normalized_sha256"),
            "previous_normalized_sha256",
        ),
        changed=changed,
        condition_met=_required_bool(payload, "condition_met"),
        refresh_disposition=RefreshDisposition(
            _required_payload_text(payload, "refresh_disposition")
        ),
    )


def _inactive_result(
    disposition: MonitorRunDisposition,
    occurrence_id: str,
    scheduled_for: datetime,
) -> MonitorRunResult:
    return MonitorRunResult(
        disposition=disposition,
        occurrence_id=occurrence_id,
        scheduled_for=scheduled_for,
        observation=None,
        previous_normalized_sha256=None,
        changed=None,
        condition_met=False,
        refresh_disposition=None,
    )


def _observation_from_payload(payload: dict[str, object]) -> NormalizedObservation:
    return NormalizedObservation(
        source_id=_required_payload_text(payload, "source_id"),
        workspace_id=_required_payload_text(payload, "workspace_id"),
        declared_locator=_required_payload_text(payload, "declared_locator"),
        resolved_locator=_required_payload_text(payload, "resolved_locator"),
        snapshot_id=_required_payload_text(payload, "snapshot_id"),
        document_id=_required_payload_text(payload, "document_id"),
        normalized_sha256=_required_payload_text(payload, "normalized_sha256"),
        source_observed_at=_required_payload_text(payload, "source_observed_at"),
    )


def _required_payload_text(payload: dict[str, object], key: str) -> str:
    return _required_text(payload.get(key), f"monitor payload {key}")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"monitor payload {key} must be a string or null")
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
