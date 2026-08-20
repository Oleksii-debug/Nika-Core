from __future__ import annotations

import hashlib
from dataclasses import dataclass

from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition
from nika_core.research.models import (
    BlobArtifact,
    RefreshDisposition,
    RefreshJobSummary,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.pagination import (
    PaginationDiscovery,
    PaginationPolicy,
    discover_html_pagination,
    discover_json_pagination,
    extend_pagination_frontier,
)
from nika_core.research.web_service import HttpResearchService


@dataclass(frozen=True, slots=True)
class _FrontierItem:
    source_id: str
    url: str


def _page_source_id(root_source_id: str, url: str) -> str:
    digest = hashlib.sha256(f"{root_source_id}\0{url}".encode()).hexdigest()
    return f"research.page.{digest}"


def _policy_to_payload(policy: PaginationPolicy) -> dict[str, object]:
    return {
        "max_pages": policy.max_pages,
        "max_discovered_links_per_page": policy.max_discovered_links_per_page,
        "same_origin_only": policy.same_origin_only,
        "json_next_fields": list(policy.json_next_fields),
    }


def _policy_from_payload(payload: object) -> PaginationPolicy:
    if not isinstance(payload, dict):
        raise TypeError("pagination policy payload is invalid")
    fields = payload.get("json_next_fields")
    if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
        raise TypeError("pagination json_next_fields payload is invalid")
    return PaginationPolicy(
        max_pages=int(payload.get("max_pages", 50)),
        max_discovered_links_per_page=int(payload.get("max_discovered_links_per_page", 8)),
        same_origin_only=bool(payload.get("same_origin_only", True)),
        json_next_fields=tuple(fields),
    )


class PaginatedResearchRefreshService:
    """Durable breadth-first static pagination on top of the existing HTTP refresh engine.

    Every discovered page is registered as an ordinary persisted HTTP source. That deliberately
    reuses the existing ETag/Last-Modified, retry, SSRF, content-hash, extraction and last-good
    behavior instead of creating a second crawler/cache. The frontier itself is a normal Nika
    task/checkpoint so restart resumes after the last completed page.
    """

    AGENT_ID = "research.http.paginated_refresh"
    CHECKPOINT_STAGE = "research.http.paginated_refresh.progress"

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
        root_source_id: str,
        policy: PaginationPolicy | None = None,
    ) -> str:
        root = self._network.get_source(root_source_id)
        active = policy or PaginationPolicy()
        task = self._tasks.create(
            workspace_id=root.workspace_id,
            agent_id=self.AGENT_ID,
            payload={
                "root_source_id": root_source_id,
                "pagination_policy": _policy_to_payload(active),
            },
        )
        self._tasks.transition(task.task_id, TaskState.READY)
        return task.task_id

    def _task_policy(self, task_id: str) -> PaginationPolicy:
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a paginated Research refresh job")
        return _policy_from_payload(task.payload.get("pagination_policy"))

    def _initial_frontier(
        self,
        task_id: str,
    ) -> tuple[list[_FrontierItem], int, int, int, int]:
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a paginated Research refresh job")
        root_source_id = str(task.payload.get("root_source_id", ""))
        if not root_source_id:
            raise ValueError("paginated Research task has no root source")
        root = self._network.get_source(root_source_id)
        checkpoint = self._checkpoints.latest(task_id)
        if checkpoint is None:
            return [_FrontierItem(root.source_id, root.url)], 0, 0, 0, 0
        if checkpoint.stage != self.CHECKPOINT_STAGE:
            raise ValueError("unexpected checkpoint stage for paginated Research refresh job")
        raw_frontier = checkpoint.payload.get("frontier")
        if not isinstance(raw_frontier, list):
            raise TypeError("paginated Research checkpoint frontier is invalid")
        frontier: list[_FrontierItem] = []
        for item in raw_frontier:
            if not isinstance(item, dict):
                raise TypeError("paginated Research checkpoint item is invalid")
            source_id = item.get("source_id")
            url = item.get("url")
            valid_source_id = isinstance(source_id, str) and bool(source_id)
            valid_url = isinstance(url, str) and bool(url)
            if not valid_source_id or not valid_url:
                raise ValueError("paginated Research checkpoint item fields are invalid")
            frontier.append(_FrontierItem(source_id, url))
        next_index = int(checkpoint.payload.get("next_index", 0))
        if not frontier or next_index < 0 or next_index > len(frontier):
            raise ValueError("paginated Research checkpoint index is outside frontier")
        return (
            frontier,
            next_index,
            int(checkpoint.payload.get("changed", 0)),
            int(checkpoint.payload.get("unchanged", 0)),
            int(checkpoint.payload.get("failed", 0)),
        )

    def _save_progress(
        self,
        task_id: str,
        *,
        frontier: list[_FrontierItem],
        next_index: int,
        changed: int,
        unchanged: int,
        failed: int,
    ) -> None:
        self._checkpoints.save(
            task_id=task_id,
            stage=self.CHECKPOINT_STAGE,
            payload={
                "frontier": [
                    {"source_id": item.source_id, "url": item.url} for item in frontier
                ],
                "next_index": next_index,
                "changed": changed,
                "unchanged": unchanged,
                "failed": failed,
            },
        )

    def _discover(
        self,
        source_id: str,
        policy: PaginationPolicy,
    ) -> PaginationDiscovery | None:
        """Read the latest stored static payload; never performs another network request."""
        state = self._network.get_source(source_id)
        store = self._network._store
        with store.connection() as conn:
            row = conn.execute(
                """SELECT s.artifact_id, s.media_type, s.observed_at, a.workspace_id,
                    a.raw_sha256, a.byte_size, a.storage_relpath
                FROM research_http_snapshots s
                JOIN corpus_artifacts a ON a.artifact_id=s.artifact_id
                WHERE s.source_id=?
                ORDER BY s.observed_at DESC
                LIMIT 1""",
                (source_id,),
            ).fetchone()
        if row is None or row["media_type"] not in {"text/html", "application/json"}:
            return None
        artifact = BlobArtifact(
            artifact_id=row["artifact_id"],
            workspace_id=row["workspace_id"],
            raw_sha256=row["raw_sha256"],
            byte_size=row["byte_size"],
            storage_relpath=row["storage_relpath"],
        )
        path = self._web._blobs.resolve(artifact)
        payload = path.read_bytes()
        page_url = state.final_url or state.url
        try:
            if row["media_type"] == "text/html":
                return discover_html_pagination(
                    page_url,
                    payload.decode("utf-8-sig", errors="replace"),
                    policy=policy,
                )
            return discover_json_pagination(page_url, payload, policy=policy)
        except (UnicodeError, ValueError):
            return None

    def _extend_frontier(
        self,
        *,
        root_source_id: str,
        workspace_id: str,
        frontier: list[_FrontierItem],
        current_index: int,
        discovery: PaginationDiscovery,
        policy: PaginationPolicy,
    ) -> None:
        frontier[current_index] = _FrontierItem(
            frontier[current_index].source_id,
            discovery.page_url,
        )
        visited = tuple(item.url for item in frontier[: current_index + 1])
        remaining = tuple(item.url for item in frontier[current_index + 1 :])
        expanded = extend_pagination_frontier(
            visited_urls=visited,
            queued_urls=remaining,
            discovery=discovery,
            policy=policy,
        )
        existing = {item.url: item for item in frontier[current_index + 1 :]}
        replacement: list[_FrontierItem] = []
        for url in expanded:
            item = existing.get(url)
            if item is None:
                source_id = _page_source_id(root_source_id, url)
                self._web.register_source(
                    SourceSpec(source_id, workspace_id, SourceKind.HTTP, url)
                )
                item = _FrontierItem(source_id, url)
            replacement.append(item)
        frontier[current_index + 1 :] = replacement

    def summary(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        frontier, next_index, changed, unchanged, failed = self._initial_frontier(task_id)
        return RefreshJobSummary(
            task_id=task_id,
            state=task.state.value.casefold(),
            processed=next_index,
            total=len(frontier),
            changed=changed,
            unchanged=unchanged,
            failed=failed,
        )

    def run(self, task_id: str) -> RefreshJobSummary:
        task = self._tasks.get(task_id)
        if task.agent_id != self.AGENT_ID:
            raise ValueError("task is not a paginated Research refresh job")
        if task.state is TaskState.READY:
            self._tasks.transition(task_id, TaskState.RUNNING)
        elif task.state is not TaskState.RUNNING:
            return self.summary(task_id)

        policy = self._task_policy(task_id)
        frontier, next_index, changed, unchanged, failed = self._initial_frontier(task_id)
        root_source_id = str(task.payload["root_source_id"])
        workspace_id = task.workspace_id

        while next_index < len(frontier):
            current = self._tasks.get(task_id)
            if current.state in {TaskState.PAUSED, TaskState.CANCELLED}:
                return self.summary(task_id)
            if current.state is not TaskState.RUNNING:
                raise ValueError(
                    f"paginated Research refresh cannot continue from {current.state.value}"
                )
            item = frontier[next_index]
            result = self._web.refresh_source(item.source_id, task_id=task_id)
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

            if result.disposition in {
                RefreshDisposition.CHANGED,
                RefreshDisposition.UNCHANGED,
                RefreshDisposition.NOT_MODIFIED,
            }:
                discovery = self._discover(item.source_id, policy)
                if discovery is not None:
                    self._extend_frontier(
                        root_source_id=root_source_id,
                        workspace_id=workspace_id,
                        frontier=frontier,
                        current_index=next_index,
                        discovery=discovery,
                        policy=policy,
                    )
            next_index += 1
            self._save_progress(
                task_id,
                frontier=frontier,
                next_index=next_index,
                changed=changed,
                unchanged=unchanged,
                failed=failed,
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
