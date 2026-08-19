from __future__ import annotations

from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition
from nika_core.research.models import RefreshDisposition, RefreshJobSummary
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.web_service import HttpResearchService


class ResearchRefreshService:
    AGENT_ID = "research.http.refresh"
    CHECKPOINT_STAGE = "research.http.refresh.progress"

    def __init__(
        self,
        *,
        tasks: TaskQueue,
        checkpoints: CheckpointService,
        network_repository: NetworkResearchRepository,
        web: HttpResearchService,
    ) -> None:
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._network = network_repository
        self._web = web

    def create_job(
        self,
        *,
        workspace_id: str,
        source_ids: tuple[str, ...] | None = None,
    ) -> str:
        sources = self._network.list_sources(workspace_id, source_ids=source_ids)
        ordered_ids = tuple(source.source_id for source in sources)
        task = self._tasks.create(
            workspace_id=workspace_id,
            agent_id=self.AGENT_ID,
            payload={"source_ids": list(ordered_ids)},
        )
        self._tasks.transition(task.task_id, TaskState.READY)
        return task.task_id

    def _progress(self, task_id: str) -> tuple[int, int, int, int]:
        checkpoint = self._checkpoints.latest(task_id)
        if checkpoint is None:
            return 0, 0, 0, 0
        if checkpoint.stage != self.CHECKPOINT_STAGE:
            raise ValueError("unexpected checkpoint stage for Research refresh job")
        payload = checkpoint.payload
        return (
            int(payload.get("next_index", 0)),
            int(payload.get("changed", 0)),
            int(payload.get("unchanged", 0)),
            int(payload.get("failed", 0)),
        )

    def summary(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a Research HTTP refresh job")
        source_ids = tuple(str(item) for item in task.payload.get("source_ids", []))
        next_index, changed, unchanged, failed = self._progress(task_id)
        return RefreshJobSummary(
            task_id=task_id,
            state=task.state.value.casefold(),
            processed=next_index,
            total=len(source_ids),
            changed=changed,
            unchanged=unchanged,
            failed=failed,
        )

    def run(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a Research HTTP refresh job")
        if task.state is TaskState.READY:
            self._tasks.transition(task_id, TaskState.RUNNING)
        elif task.state is not TaskState.RUNNING:
            return self.summary(task_id)

        source_ids = tuple(str(item) for item in task.payload.get("source_ids", []))
        next_index, changed, unchanged, failed = self._progress(task_id)
        if next_index < 0 or next_index > len(source_ids):
            raise ValueError("Research refresh checkpoint index is outside source set")

        for index in range(next_index, len(source_ids)):
            current = self._tasks.get(task_id)
            if current.state in {TaskState.PAUSED, TaskState.CANCELLED}:
                return self.summary(task_id)
            if current.state is not TaskState.RUNNING:
                raise ValueError(f"Research refresh cannot continue from {current.state.value}")
            result = self._web.refresh_source(source_ids[index], task_id=task_id)
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
            self._checkpoints.save(
                task_id=task_id,
                stage=self.CHECKPOINT_STAGE,
                payload={
                    "next_index": next_index,
                    "changed": changed,
                    "unchanged": unchanged,
                    "failed": failed,
                },
            )

        current = self._tasks.get(task_id)
        if current.state is TaskState.RUNNING:
            self._tasks.transition(task_id, TaskState.COMPLETED)
        return self.summary(task_id)

    def pause(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if can_transition(task.state, TaskState.PAUSED):
            self._tasks.transition(task_id, TaskState.PAUSED)
        return self.summary(task_id)

    def resume(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if can_transition(task.state, TaskState.READY):
            self._tasks.transition(task_id, TaskState.READY)
        return self.run(task_id)

    def cancel(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if can_transition(task.state, TaskState.CANCELLED):
            self._tasks.transition(task_id, TaskState.CANCELLED)
        return self.summary(task_id)
