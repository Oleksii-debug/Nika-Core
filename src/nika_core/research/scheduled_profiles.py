from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_state import TaskState
from nika_core.research.models import ResearchEvidence, ResearchResultItem
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.profile_jobs import ResearchProfileRunService, ResearchProfileRunSummary
from nika_core.research.profiles import ResearchProfileRepository
from nika_core.scheduler import ScheduledJob, SchedulerPort, TriggerKind


class ResearchDeltaKind(StrEnum):
    NEW = "new"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class ResearchDeltaItem:
    ordinal: int
    kind: ResearchDeltaKind
    item: ResearchResultItem
    previous_document_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchProfileDelta:
    task_id: str
    series_id: str
    result_set_id: str
    previous_result_set_id: str | None
    items: tuple[ResearchDeltaItem, ...]


@dataclass(frozen=True, slots=True)
class ScheduledResearchRun:
    run: ResearchProfileRunSummary
    delta: ResearchProfileDelta | None


class ScheduledResearchProfileService:
    """Compose Research profile runs with Nika's existing SchedulerPort and durable deltas."""

    ACTION_ID = "research.profile.run"

    def __init__(
        self,
        *,
        store: SQLiteStore,
        scheduler: SchedulerPort,
        profiles: ResearchProfileRepository,
        runs: ResearchProfileRunService,
        network_repository: NetworkResearchRepository,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._profiles = profiles
        self._runs = runs
        self._network = network_repository

    def upsert_schedule(
        self,
        *,
        schedule_id: str,
        profile_id: str,
        trigger_kind: TriggerKind,
        trigger: dict[str, Any],
        profile_version: int | None = None,
        enabled: bool = True,
        coalesce: bool = True,
        misfire_grace_seconds: int | None = 60,
    ) -> ScheduledJob:
        series_id = self._required_text(schedule_id, "schedule_id")
        profile_key = self._required_text(profile_id, "profile_id")
        if profile_version is not None and (
            not isinstance(profile_version, int)
            or isinstance(profile_version, bool)
            or profile_version < 1
        ):
            raise ValueError("profile_version must be a positive integer or None")
        self._profiles.load_profile(profile_key, profile_version)
        job = ScheduledJob(
            job_id=series_id,
            action_id=self.ACTION_ID,
            trigger_kind=trigger_kind,
            trigger=dict(trigger),
            payload={
                "series_id": series_id,
                "profile_id": profile_key,
                "profile_version": profile_version,
            },
            enabled=enabled,
            coalesce=coalesce,
            max_instances=1,
            misfire_grace_seconds=misfire_grace_seconds,
        )
        self._scheduler.upsert(job)
        return job

    def action_handler(self, payload: dict[str, Any]) -> None:
        self.run_scheduled(payload)

    def run_scheduled(self, payload: dict[str, Any]) -> ScheduledResearchRun:
        series_id = self._required_text(payload.get("series_id"), "series_id")
        profile_id = self._required_text(payload.get("profile_id"), "profile_id")
        version = payload.get("profile_version")
        if version is not None and (
            not isinstance(version, int) or isinstance(version, bool) or version < 1
        ):
            raise ValueError("profile_version must be a positive integer or None")

        outstanding = self._outstanding_task(series_id)
        if outstanding is None:
            task_id = self._runs.create_job(profile_id, version)
            with self._store.connection() as conn:
                conn.execute(
                    """INSERT INTO research_profile_series_tasks(series_id, task_id, created_at)
                    VALUES (?, ?, ?)""",
                    (series_id, task_id, datetime.now(UTC).isoformat()),
                )
        else:
            task_id, state = outstanding
            if state is TaskState.PAUSED:
                return ScheduledResearchRun(run=self._runs.summary(task_id), delta=None)

        summary = self._runs.run(task_id)
        if summary.state != TaskState.COMPLETED.value.casefold():
            return ScheduledResearchRun(run=summary, delta=None)
        return ScheduledResearchRun(run=summary, delta=self._record_delta(series_id, summary))

    def delta_for_task(self, task_id: str) -> ResearchProfileDelta:
        with self._store.connection() as conn:
            history = conn.execute(
                "SELECT * FROM research_profile_run_history WHERE task_id=?",
                (task_id,),
            ).fetchone()
            rows = conn.execute(
                """SELECT ordinal, change_kind, document_id, previous_document_id
                FROM research_profile_delta_items WHERE task_id=? ORDER BY ordinal""",
                (task_id,),
            ).fetchall()
        if history is None:
            raise KeyError(f"unknown research profile run delta: {task_id}")
        result_set = self._network.get_result_set(history["result_set_id"])
        by_document = {item.document_id: item for item in result_set.items}
        items = tuple(
            ResearchDeltaItem(
                ordinal=int(row["ordinal"]),
                kind=ResearchDeltaKind(row["change_kind"]),
                item=by_document[row["document_id"]],
                previous_document_id=row["previous_document_id"],
            )
            for row in rows
        )
        return ResearchProfileDelta(
            task_id=task_id,
            series_id=history["series_id"],
            result_set_id=history["result_set_id"],
            previous_result_set_id=history["previous_result_set_id"],
            items=items,
        )

    @staticmethod
    def render_delta_text(delta: ResearchProfileDelta) -> str:
        new_count = sum(item.kind is ResearchDeltaKind.NEW for item in delta.items)
        changed_count = sum(item.kind is ResearchDeltaKind.CHANGED for item in delta.items)
        lines = [
            "Research recurring update",
            f"Series: {delta.series_id}",
            f"New: {new_count}",
            f"Changed: {changed_count}",
            f"Updates: {len(delta.items)}",
        ]
        if not delta.items:
            lines.append("No new or changed matching results.")
            return "\n".join(lines) + "\n"
        lines.append("")
        for index, delta_item in enumerate(delta.items, start=1):
            item = delta_item.item
            lines.extend(
                (
                    f"{index}. {delta_item.kind.value.upper()}: {item.title}",
                    f"Snippet: {item.snippet}",
                    "Sources:",
                )
            )
            for evidence in item.evidence:
                lines.append(f"- {evidence.source_kind.value}: {evidence.locator}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _outstanding_task(self, series_id: str) -> tuple[str, TaskState] | None:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT t.task_id, t.state, h.task_id AS history_task_id
                FROM research_profile_series_tasks b
                JOIN tasks t ON t.task_id=b.task_id
                LEFT JOIN research_profile_run_history h ON h.task_id=t.task_id
                WHERE b.series_id=?
                ORDER BY b.created_at, t.task_id""",
                (series_id,),
            ).fetchall()
        for row in rows:
            state = TaskState(row["state"])
            if state not in {TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED}:
                return row["task_id"], state
            if state is TaskState.COMPLETED and row["history_task_id"] is None:
                return row["task_id"], state
        return None

    def _record_delta(
        self,
        series_id: str,
        summary: ResearchProfileRunSummary,
    ) -> ResearchProfileDelta:
        if summary.result_set_id is None:
            raise ValueError("completed Research profile run has no result set")
        try:
            return self.delta_for_task(summary.task_id)
        except KeyError:
            pass

        current = self._network.get_result_set(summary.result_set_id)
        with self._store.connection() as conn:
            previous_row = conn.execute(
                """SELECT result_set_id FROM research_profile_run_history
                WHERE series_id=? ORDER BY created_at DESC, task_id DESC LIMIT 1""",
                (series_id,),
            ).fetchone()
        previous_id = previous_row["result_set_id"] if previous_row is not None else None
        previous = self._network.get_result_set(previous_id) if previous_id is not None else None
        delta_items = self._classify_delta(current.items, previous.items if previous else ())

        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_profile_run_history(
                    task_id, series_id, profile_id, profile_version, source_set_id,
                    source_set_version, result_set_id, previous_result_set_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    summary.task_id,
                    series_id,
                    summary.profile_id,
                    summary.profile_version,
                    summary.source_set_id,
                    summary.source_set_version,
                    summary.result_set_id,
                    previous_id,
                    current.created_at,
                ),
            )
            for ordinal, delta_item in enumerate(delta_items):
                conn.execute(
                    """INSERT INTO research_profile_delta_items(
                        task_id, ordinal, change_kind, document_id, previous_document_id
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        summary.task_id,
                        ordinal,
                        delta_item.kind.value,
                        delta_item.item.document_id,
                        delta_item.previous_document_id,
                    ),
                )
        return self.delta_for_task(summary.task_id)

    @classmethod
    def _classify_delta(
        cls,
        current: tuple[ResearchResultItem, ...],
        previous: tuple[ResearchResultItem, ...],
    ) -> tuple[ResearchDeltaItem, ...]:
        prior_documents = {item.document_id for item in previous}
        prior_by_origin: dict[tuple[str, str, str], ResearchResultItem] = {}
        for item in previous:
            for evidence in item.evidence:
                prior_by_origin.setdefault(cls._origin_key(evidence), item)

        delta: list[ResearchDeltaItem] = []
        for item in current:
            if item.document_id in prior_documents:
                continue
            predecessor = next(
                (
                    prior_by_origin[cls._origin_key(evidence)]
                    for evidence in item.evidence
                    if cls._origin_key(evidence) in prior_by_origin
                ),
                None,
            )
            kind = ResearchDeltaKind.CHANGED if predecessor is not None else ResearchDeltaKind.NEW
            delta.append(
                ResearchDeltaItem(
                    ordinal=len(delta),
                    kind=kind,
                    item=item,
                    previous_document_id=(predecessor.document_id if predecessor is not None else None),
                )
            )
        return tuple(delta)

    @staticmethod
    def _origin_key(evidence: ResearchEvidence) -> tuple[str, str, str]:
        return evidence.source_kind.value, evidence.source_id, evidence.locator

    @staticmethod
    def _required_text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
        return value.strip()
