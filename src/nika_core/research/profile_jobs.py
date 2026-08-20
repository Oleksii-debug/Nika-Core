from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition
from nika_core.research.models import RefreshDisposition, SourceKind
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.profiles import ResearchProfile, ResearchProfileRepository, ResearchSourceSet
from nika_core.research.query import (
    DeterministicResearchQueryService,
    ResearchQuerySpec,
    ResearchSearchFilters,
)
from nika_core.research.web_service import HttpResearchService


@dataclass(frozen=True, slots=True)
class ResearchProfileRunSummary:
    task_id: str
    state: str
    profile_id: str
    profile_version: int
    source_set_id: str
    source_set_version: int
    processed: int
    total: int
    changed: int
    unchanged: int
    failed: int
    result_set_id: str | None
    result_count: int


class ResearchProfileRunService:
    """Durable profile refresh -> deterministic query orchestration on the canonical TaskQueue."""

    AGENT_ID = "research.profile.run"
    CHECKPOINT_STAGE = "research.profile.run.progress"

    def __init__(
        self,
        *,
        tasks: TaskQueue,
        checkpoints: CheckpointService,
        profiles: ResearchProfileRepository,
        network_repository: NetworkResearchRepository,
        query_service: DeterministicResearchQueryService,
        web: HttpResearchService,
    ) -> None:
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._profiles = profiles
        self._network = network_repository
        self._query = query_service
        self._web = web

    def create_job(self, profile_id: str, version: int | None = None) -> str:
        profile = self._profiles.load_profile(profile_id, version)
        source_set = self._profiles.load_source_set(
            profile.source_set_id,
            profile.source_set_version,
        )
        http_source_ids = tuple(
            source.source_id for source in source_set.sources if source.kind is SourceKind.HTTP
        )
        task = self._tasks.create(
            workspace_id=profile.workspace_id,
            agent_id=self.AGENT_ID,
            payload={
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "source_set_id": source_set.source_set_id,
                "source_set_version": source_set.version,
                "http_source_ids": list(http_source_ids),
            },
        )
        self._tasks.transition(task.task_id, TaskState.READY)
        return task.task_id

    def summary(self, task_id: str) -> ResearchProfileRunSummary:
        task = self._task(task_id)
        payload = task.payload
        http_source_ids = self._http_source_ids(payload)
        next_index, changed, unchanged, failed, result_set_id = self._progress(task_id)
        result_count = 0
        if result_set_id is not None:
            result_count = len(self._network.get_result_set(result_set_id).items)
        return ResearchProfileRunSummary(
            task_id=task_id,
            state=task.state.value.casefold(),
            profile_id=self._payload_text(payload, "profile_id"),
            profile_version=self._payload_int(payload, "profile_version"),
            source_set_id=self._payload_text(payload, "source_set_id"),
            source_set_version=self._payload_int(payload, "source_set_version"),
            processed=next_index,
            total=len(http_source_ids),
            changed=changed,
            unchanged=unchanged,
            failed=failed,
            result_set_id=result_set_id,
            result_count=result_count,
        )

    def run(self, task_id: str) -> ResearchProfileRunSummary:
        task = self._task(task_id)
        if task.state is TaskState.READY:
            self._tasks.transition(task_id, TaskState.RUNNING)
        elif task.state is not TaskState.RUNNING:
            return self.summary(task_id)

        task = self._task(task_id)
        profile, source_set = self._load_pinned_definitions(task.payload)
        http_source_ids = self._http_source_ids(task.payload)
        next_index, changed, unchanged, failed, result_set_id = self._progress(task_id)
        if next_index < 0 or next_index > len(http_source_ids):
            raise ValueError("Research profile checkpoint index is outside source set")

        for index in range(next_index, len(http_source_ids)):
            current = self._task(task_id)
            if current.state in {TaskState.PAUSED, TaskState.CANCELLED}:
                return self.summary(task_id)
            if current.state is not TaskState.RUNNING:
                raise ValueError(f"Research profile run cannot continue from {current.state.value}")
            result = self._web.refresh_source(http_source_ids[index], task_id=task_id)
            if result.disposition in {
                RefreshDisposition.CHANGED,
                RefreshDisposition.DYNAMIC_REQUIRED,
            }:
                changed += 1
            elif result.disposition in {
                RefreshDisposition.UNCHANGED,
                RefreshDisposition.NOT_MODIFIED,
            }:
                unchanged += 1
            else:
                failed += 1
            next_index = index + 1
            self._save_progress(
                task_id,
                next_index=next_index,
                changed=changed,
                unchanged=unchanged,
                failed=failed,
                result_set_id=None,
            )

        current = self._task(task_id)
        if current.state in {TaskState.PAUSED, TaskState.CANCELLED}:
            return self.summary(task_id)
        if current.state is not TaskState.RUNNING:
            raise ValueError(f"Research profile run cannot query from {current.state.value}")

        stable_result_set_id = result_set_id or self._stable_result_set_id(task_id)
        filters = ResearchSearchFilters(
            source_ids=tuple(source.source_id for source in source_set.sources),
            source_kinds=profile.filters.source_kinds,
            media_types=profile.filters.media_types,
            freshness=profile.filters.freshness,
        )
        execution = self._query.execute(
            ResearchQuerySpec(
                workspace_id=profile.workspace_id,
                text=profile.query_text,
                mode=profile.query_mode,
                filters=filters,
                limit=profile.result_limit,
            ),
            result_set_id=stable_result_set_id,
        )
        self._save_progress(
            task_id,
            next_index=len(http_source_ids),
            changed=changed,
            unchanged=unchanged,
            failed=failed,
            result_set_id=execution.result_set.result_set_id,
        )
        if self._task(task_id).state is TaskState.RUNNING:
            self._tasks.transition(task_id, TaskState.COMPLETED)
        return self.summary(task_id)

    def pause(self, task_id: str) -> ResearchProfileRunSummary:
        task = self._task(task_id)
        if can_transition(task.state, TaskState.PAUSED):
            self._tasks.transition(task_id, TaskState.PAUSED)
        return self.summary(task_id)

    def resume(self, task_id: str) -> ResearchProfileRunSummary:
        task = self._task(task_id)
        if can_transition(task.state, TaskState.READY):
            self._tasks.transition(task_id, TaskState.READY)
        return self.run(task_id)

    def cancel(self, task_id: str) -> ResearchProfileRunSummary:
        task = self._task(task_id)
        if can_transition(task.state, TaskState.CANCELLED):
            self._tasks.transition(task_id, TaskState.CANCELLED)
        return self.summary(task_id)

    @staticmethod
    def render_text(summary: ResearchProfileRunSummary) -> str:
        lines = [
            "Research profile run",
            f"Profile: {summary.profile_id} v{summary.profile_version}",
            f"Source set: {summary.source_set_id} v{summary.source_set_version}",
            f"State: {summary.state}",
            f"HTTP refresh: {summary.processed}/{summary.total}",
            f"Changed: {summary.changed}",
            f"Unchanged: {summary.unchanged}",
            f"Failed refreshes: {summary.failed}",
            f"Results: {summary.result_count}",
        ]
        if summary.result_set_id is not None:
            lines.append(f"Result set: {summary.result_set_id}")
        return "\n".join(lines) + "\n"

    def _task(self, task_id: str):
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a Research profile run")
        return task

    def _load_pinned_definitions(
        self,
        payload: dict[str, object],
    ) -> tuple[ResearchProfile, ResearchSourceSet]:
        profile = self._profiles.load_profile(
            self._payload_text(payload, "profile_id"),
            self._payload_int(payload, "profile_version"),
        )
        source_set = self._profiles.load_source_set(
            self._payload_text(payload, "source_set_id"),
            self._payload_int(payload, "source_set_version"),
        )
        if profile.source_set_id != source_set.source_set_id:
            raise ValueError("pinned profile/source-set identity mismatch")
        if profile.source_set_version != source_set.version:
            raise ValueError("pinned profile/source-set version mismatch")
        return profile, source_set

    def _progress(self, task_id: str) -> tuple[int, int, int, int, str | None]:
        checkpoint = self._checkpoints.latest(task_id)
        if checkpoint is None:
            return 0, 0, 0, 0, None
        if checkpoint.stage != self.CHECKPOINT_STAGE:
            raise ValueError("unexpected checkpoint stage for Research profile run")
        payload = checkpoint.payload
        result_set_id = payload.get("result_set_id")
        if result_set_id is not None and not isinstance(result_set_id, str):
            raise TypeError("stored result_set_id must be a string or null")
        return (
            int(payload.get("next_index", 0)),
            int(payload.get("changed", 0)),
            int(payload.get("unchanged", 0)),
            int(payload.get("failed", 0)),
            result_set_id,
        )

    def _save_progress(
        self,
        task_id: str,
        *,
        next_index: int,
        changed: int,
        unchanged: int,
        failed: int,
        result_set_id: str | None,
    ) -> None:
        self._checkpoints.save(
            task_id=task_id,
            stage=self.CHECKPOINT_STAGE,
            payload={
                "next_index": next_index,
                "changed": changed,
                "unchanged": unchanged,
                "failed": failed,
                "result_set_id": result_set_id,
            },
        )

    @staticmethod
    def _stable_result_set_id(task_id: str) -> str:
        return uuid5(NAMESPACE_URL, f"nika:research-profile-run:{task_id}").hex

    @staticmethod
    def _payload_text(payload: dict[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Research profile task payload has invalid {key}")
        return value

    @staticmethod
    def _payload_int(payload: dict[str, object], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"Research profile task payload has invalid {key}")
        return value

    @classmethod
    def _http_source_ids(cls, payload: dict[str, object]) -> tuple[str, ...]:
        raw = payload.get("http_source_ids")
        if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
            raise ValueError("Research profile task payload has invalid http_source_ids")
        values = tuple(value.strip() for value in raw)
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("Research profile task payload has invalid http_source_ids")
        return values
