from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    RefreshResult,
    ResearchRefreshService,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

PUBLIC_IP = "93.184.216.34"


def _resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return (PUBLIC_IP,)


def _base(tmp_path: Path) -> tuple[SQLiteStore, ResearchRepository, NetworkResearchRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    network.register_source(
        SourceSpec("a", "ws", SourceKind.HTTP, "https://example.com/a")
    )
    network.register_source(
        SourceSpec("b", "ws", SourceKind.HTTP, "https://example.com/b")
    )
    return store, repository, network


def _http_service(
    tmp_path: Path,
    *,
    store: SQLiteStore,
    handler,
) -> HttpResearchService:
    return HttpResearchService(
        repository=ResearchRepository(store),
        network_repository=NetworkResearchRepository(store),
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=_resolver,
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )


class _CrashAfterDurableSource:
    def __init__(self, delegate: HttpResearchService, *, crash_source: str) -> None:
        self._delegate = delegate
        self._crash_source = crash_source
        self._crashed = False

    def refresh_source(self, source_id: str, *, task_id: str | None = None) -> RefreshResult:
        result = self._delegate.refresh_source(source_id, task_id=task_id)
        if source_id == self._crash_source and not self._crashed:
            self._crashed = True
            raise RuntimeError("simulated process loss after durable source")
        return result


class _CountingWeb:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh_source(self, source_id: str, *, task_id: str | None = None) -> RefreshResult:
        assert task_id is not None
        self.calls.append(source_id)
        return RefreshResult(
            source_id=source_id,
            disposition=RefreshDisposition.CHANGED,
            attempts=1,
        )


def test_restart_recovers_durable_source_without_duplicate_or_provenance_loss(
    tmp_path: Path,
) -> None:
    store, repository, network = _base(tmp_path)

    def first_handler(request: httpx.Request) -> httpx.Response:
        body = b"alpha durable evidence" if request.url.path == "/a" else b"beta evidence"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "ETag": f'"{request.url.path}"'},
            content=body,
        )

    durable_web = _http_service(tmp_path, store=store, handler=first_handler)
    first = ResearchRefreshService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        network_repository=network,
        web=_CrashAfterDurableSource(durable_web, crash_source="a"),  # type: ignore[arg-type]
    )
    task_id = first.create_job(workspace_id="ws", source_ids=("a", "b"))

    with pytest.raises(RuntimeError, match="process loss after durable source"):
        first.run(task_id)

    partial = first.summary(task_id)
    assert partial.state == "running"
    assert partial.processed == 0
    assert network.snapshot_count("a") == 1
    assert network.attempt_count("a") == 1
    hit = repository.search("ws", "alpha durable")[0]
    before_evidence = network.evidence_for_document(hit.document_id)
    assert len(before_evidence) == 1
    assert before_evidence[0].source_id == "a"
    assert before_evidence[0].locator == "https://example.com/a"

    resumed_requests: list[str] = []

    def resumed_handler(request: httpx.Request) -> httpx.Response:
        resumed_requests.append(request.url.path)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"beta evidence",
        )

    restarted_store = SQLiteStore(store.path)
    restarted = ResearchRefreshService(
        tasks=TaskQueue(restarted_store),
        checkpoints=CheckpointService(restarted_store),
        network_repository=NetworkResearchRepository(restarted_store),
        web=_http_service(tmp_path, store=restarted_store, handler=resumed_handler),
    )
    completed = restarted.run(task_id)

    restarted_network = NetworkResearchRepository(SQLiteStore(store.path))
    assert completed.state == "completed"
    assert completed.processed == 2
    assert completed.changed == 2
    assert resumed_requests == ["/b"]
    assert restarted_network.snapshot_count("a") == 1
    assert restarted_network.attempt_count("a") == 1
    assert restarted_network.evidence_for_document(hit.document_id) == before_evidence


def test_duplicate_source_ids_are_one_durable_refresh_step(tmp_path: Path) -> None:
    store, _, network = _base(tmp_path)
    web = _CountingWeb()
    jobs = ResearchRefreshService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        network_repository=network,
        web=web,  # type: ignore[arg-type]
    )

    task_id = jobs.create_job(workspace_id="ws", source_ids=("a", "a", "a"))
    completed = jobs.run(task_id)

    assert completed.total == 1
    assert completed.processed == 1
    assert web.calls == ["a"]


def test_completed_task_cannot_masquerade_with_incomplete_research_progress(
    tmp_path: Path,
) -> None:
    store, _, network = _base(tmp_path)
    tasks = TaskQueue(store)
    jobs = ResearchRefreshService(
        tasks=tasks,
        checkpoints=CheckpointService(store),
        network_repository=network,
        web=_CountingWeb(),  # type: ignore[arg-type]
    )
    task_id = jobs.create_job(workspace_id="ws", source_ids=("a",))
    tasks.transition(task_id, TaskState.RUNNING)
    tasks.transition(task_id, TaskState.COMPLETED)

    with pytest.raises(ValueError, match="incomplete durable progress"):
        jobs.summary(task_id)


def test_restart_rechecks_live_permission_before_any_remaining_network_read(
    tmp_path: Path,
) -> None:
    store, _, network = _base(tmp_path)
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    permission_id = "research-restart-permission"
    subject_id = "research-agent"
    user_id = "user"
    project_id = "project"

    def first_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=f"evidence {request.url.path}".encode(),
        )

    tasks = TaskQueue(store)
    checkpoints = CheckpointService(store)
    durable_web = _http_service(tmp_path, store=store, handler=first_handler)
    task_id_holder: dict[str, str] = {}

    def authorize(*, task, source) -> None:
        host = urlsplit(source.url).hostname
        assert host is not None
        context = PermissionContext(user_id=user_id, project_id=project_id, task_id=task.task_id)
        intent = ActionIntent(
            action_id=f"refresh:{source.source_id}",
            tool_id=ResearchRefreshService.AGENT_ID,
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            target=source.source_id,
            network_host=host,
            task_id=task.task_id,
            project_id=project_id,
            site=host,
            resource=source.source_id,
            arguments={"source_id": source.source_id},
            effect_id=f"{task.task_id}:{source.source_id}",
        )
        permissions.authorize(
            permission_id,
            StandingPermissionUse(
                subject_id=subject_id,
                context=context,
                intent=intent,
                resource_id=source.source_id,
            ),
            now=now,
        )

    first = ResearchRefreshService(
        tasks=tasks,
        checkpoints=checkpoints,
        network_repository=network,
        web=_CrashAfterDurableSource(durable_web, crash_source="a"),  # type: ignore[arg-type]
        authorization=authorize,
    )
    task_id = first.create_job(workspace_id="ws", source_ids=("a", "b"))
    task_id_holder["id"] = task_id
    permissions.grant(
        permission_id=permission_id,
        scope=StandingPermissionScope(
            subject_id=subject_id,
            context=PermissionContext(
                user_id=user_id,
                project_id=project_id,
                task_id=task_id,
            ),
            action_class=ResearchRefreshService.AGENT_ID,
            targets=("a", "b"),
            sites=("example.com",),
            resources=("a", "b"),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        ),
    )

    with pytest.raises(RuntimeError, match="process loss after durable source"):
        first.run(task_id)
    permissions.revoke(permission_id, revoked_at=now + timedelta(seconds=1))

    resumed_requests: list[str] = []

    def resumed_handler(request: httpx.Request) -> httpx.Response:
        resumed_requests.append(request.url.path)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"should not be fetched",
        )

    restarted_store = SQLiteStore(store.path)
    restarted_permissions = StandingPermissionStore(restarted_store)

    def authorize_after_restart(*, task, source) -> None:
        host = urlsplit(source.url).hostname
        assert host is not None
        restarted_permissions.authorize(
            permission_id,
            StandingPermissionUse(
                subject_id=subject_id,
                context=PermissionContext(
                    user_id=user_id,
                    project_id=project_id,
                    task_id=task.task_id,
                ),
                intent=ActionIntent(
                    action_id=f"refresh:{source.source_id}",
                    tool_id=ResearchRefreshService.AGENT_ID,
                    risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
                    target=source.source_id,
                    network_host=host,
                    task_id=task.task_id,
                    project_id=project_id,
                    site=host,
                    resource=source.source_id,
                    arguments={"source_id": source.source_id},
                    effect_id=f"{task.task_id}:{source.source_id}",
                ),
                resource_id=source.source_id,
            ),
            now=now + timedelta(seconds=2),
        )

    restarted = ResearchRefreshService(
        tasks=TaskQueue(restarted_store),
        checkpoints=CheckpointService(restarted_store),
        network_repository=NetworkResearchRepository(restarted_store),
        web=_http_service(tmp_path, store=restarted_store, handler=resumed_handler),
        authorization=authorize_after_restart,
    )

    with pytest.raises(PermissionError, match="revoked"):
        restarted.run(task_id)

    partial = restarted.summary(task_id)
    assert partial.state == "running"
    assert partial.processed == 1
    assert resumed_requests == []
    assert NetworkResearchRepository(SQLiteStore(store.path)).snapshot_count("a") == 1
