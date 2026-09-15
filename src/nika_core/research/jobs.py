from __future__ import annotations

from typing import Protocol

from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState, can_transition
from nika_core.research.models import (
    HttpSourceState,
    RefreshDisposition,
    RefreshJobSummary,
    RefreshResult,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.web_service import HttpResearchService


class ResearchRefreshAuthorizer(Protocol):
    """Live authorization boundary for a network read owned outside Research."""

    def __call__(self, *, task: TaskRecord, source: HttpSourceState) -> None: ...


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
        authorization: ResearchRefreshAuthorizer | None = None,
    ) -> None:
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._network = network_repository
        self._web = web
        self._authorization = authorization

    def create_job(
        self,
        *,
        workspace_id: str,
        source_ids: tuple[str, ...] | None = None,
    ) -> str:
        sources = self._network.list_sources(workspace_id, source_ids=source_ids)
        # A source is one durable refresh step. Preserve canonical ordering but never
        # schedule the same durable source twice merely because a caller repeated its id.
        ordered_ids = tuple(dict.fromkeys(source.source_id for source in sources))
        task = self._tasks.create(
            workspace_id=workspace_id,
            agent_id=self.AGENT_ID,
            payload={"source_ids": list(ordered_ids)},
        )
        self._tasks.transition(task.task_id, TaskState.READY)
        return task.task_id

    def _progress(
        self,
        task_id: str,
    ) -> tuple[int, int, int, int, dict[str, object] | None]:
        checkpoint = self._checkpoints.latest(task_id)
        if checkpoint is None:
            return 0, 0, 0, 0, None
        if checkpoint.stage != self.CHECKPOINT_STAGE:
            raise ValueError("unexpected checkpoint stage for Research refresh job")
        payload = checkpoint.payload
        raw_in_flight = payload.get("in_flight")
        if raw_in_flight is not None and not isinstance(raw_in_flight, dict):
            raise ValueError("Research refresh checkpoint in_flight marker is malformed")
        in_flight = dict(raw_in_flight) if raw_in_flight is not None else None
        return (
            int(payload.get("next_index", 0)),
            int(payload.get("changed", 0)),
            int(payload.get("unchanged", 0)),
            int(payload.get("failed", 0)),
            in_flight,
        )

    def _save_progress(
        self,
        *,
        task_id: str,
        next_index: int,
        changed: int,
        unchanged: int,
        failed: int,
        in_flight: dict[str, object] | None,
    ) -> None:
        self._checkpoints.save(
            task_id=task_id,
            stage=self.CHECKPOINT_STAGE,
            payload={
                "next_index": next_index,
                "changed": changed,
                "unchanged": unchanged,
                "failed": failed,
                "in_flight": in_flight,
            },
        )

    def _task_attempt_count(self, task_id: str, source_id: str) -> int:
        # HTTP attempt rows already live in the canonical Research SQLite store. This
        # narrow read is deliberately not a second run/checkpoint database.
        with self._network._store.connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS count FROM research_http_attempts
                WHERE task_id=? AND source_id=?""",
                (task_id, source_id),
            ).fetchone()
        return int(row["count"])

    def _durable_in_flight_result(
        self,
        *,
        task_id: str,
        source_id: str,
        attempts_before: int,
    ) -> RefreshResult | None:
        """Recover a source result that became durable before its checkpoint did.

        HttpResearchService records the terminal attempt and finalizes the canonical
        source state before returning. A post-marker task attempt plus a source
        ``last_attempt_at`` at/after that attempt is therefore durable evidence that
        this source step finished even if the process died before the job cursor moved.
        """
        if attempts_before < 0:
            raise ValueError("Research refresh attempt baseline cannot be negative")
        with self._network._store.connection() as conn:
            rows = conn.execute(
                """SELECT attempt_number, disposition, status_code, error_code,
                    error_message, observed_at
                FROM research_http_attempts
                WHERE task_id=? AND source_id=?
                ORDER BY observed_at, rowid""",
                (task_id, source_id),
            ).fetchall()
            state = conn.execute(
                "SELECT last_attempt_at FROM research_http_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
        if state is None:
            raise KeyError(f"unknown HTTP source: {source_id}")
        if len(rows) <= attempts_before:
            return None
        row = rows[-1]
        last_attempt_at = state["last_attempt_at"]
        if last_attempt_at is None or str(last_attempt_at) < str(row["observed_at"]):
            return None
        return RefreshResult(
            source_id=source_id,
            disposition=RefreshDisposition(row["disposition"]),
            attempts=int(row["attempt_number"]),
            status_code=row["status_code"],
            error_code=row["error_code"],
            message=row["error_message"],
        )

    @staticmethod
    def _count_result(
        result: RefreshResult,
        *,
        changed: int,
        unchanged: int,
        failed: int,
    ) -> tuple[int, int, int]:
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
        return changed, unchanged, failed

    def summary(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a Research HTTP refresh job")
        source_ids = tuple(str(item) for item in task.payload.get("source_ids", []))
        next_index, changed, unchanged, failed, _ = self._progress(task_id)
        if task.state is TaskState.COMPLETED and next_index != len(source_ids):
            raise ValueError("completed Research refresh task has incomplete durable progress")
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
            task = self._tasks.get(task_id)
        elif task.state is not TaskState.RUNNING:
            return self.summary(task_id)

        source_ids = tuple(str(item) for item in task.payload.get("source_ids", []))
        next_index, changed, unchanged, failed, in_flight = self._progress(task_id)
        if next_index < 0 or next_index > len(source_ids):
            raise ValueError("Research refresh checkpoint index is outside source set")

        for index in range(next_index, len(source_ids)):
            current = self._tasks.get(task_id)
            if current.state in {TaskState.PAUSED, TaskState.CANCELLED}:
                return self.summary(task_id)
            if current.state is not TaskState.RUNNING:
                raise ValueError(f"Research refresh cannot continue from {current.state.value}")

            source_id = source_ids[index]
            result: RefreshResult | None = None
            if in_flight is not None:
                try:
                    marker_index = int(in_flight["index"])
                    marker_source_id = str(in_flight["source_id"])
                    attempts_before = int(in_flight["task_attempts_before"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("Research refresh checkpoint in_flight marker is malformed") from exc
                if marker_index != index or marker_source_id != source_id:
                    raise ValueError("Research refresh checkpoint in_flight marker conflicts with cursor")
                result = self._durable_in_flight_result(
                    task_id=task_id,
                    source_id=source_id,
                    attempts_before=attempts_before,
                )

            if result is None:
                attempts_before = self._task_attempt_count(task_id, source_id)
                in_flight = {
                    "index": index,
                    "source_id": source_id,
                    "task_attempts_before": attempts_before,
                }
                self._save_progress(
                    task_id=task_id,
                    next_index=next_index,
                    changed=changed,
                    unchanged=unchanged,
                    failed=failed,
                    in_flight=in_flight,
                )
                source = self._network.get_source(source_id)
                # Never checkpoint an authorization decision. Any network operation that
                # still has to happen is authorized against live state immediately before use.
                if self._authorization is not None:
                    self._authorization(task=current, source=source)
                result = self._web.refresh_source(source_id, task_id=task_id)

            changed, unchanged, failed = self._count_result(
                result,
                changed=changed,
                unchanged=unchanged,
                failed=failed,
            )
            next_index = index + 1
            in_flight = None
            self._save_progress(
                task_id=task_id,
                next_index=next_index,
                changed=changed,
                unchanged=unchanged,
                failed=failed,
                in_flight=None,
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
