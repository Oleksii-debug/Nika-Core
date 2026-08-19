from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research import (
    ContentAddressedBlobStore,
    FreshnessState,
    HttpFetchPolicy,
    HttpResearchService,
    HttpxResearchFetcher,
    LocalCorpusService,
    NetworkResearchRepository,
    RefreshDisposition,
    RefreshResult,
    ResearchRefreshService,
    ResearchRepository,
    ResearchResultService,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)


PUBLIC_IP = "93.184.216.34"
SECOND_PUBLIC_IP = "1.1.1.1"


def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del port
    if host == "other.example":
        return (SECOND_PUBLIC_IP,)
    return (PUBLIC_IP,)


def _store(tmp_path: Path) -> tuple[SQLiteStore, ResearchRepository, NetworkResearchRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    return store, repository, NetworkResearchRepository(store)


def _source(source_id: str = "web-1", url: str = "https://example.com/page") -> SourceSpec:
    return SourceSpec(source_id, "ws", SourceKind.HTTP, url)


def _service(
    tmp_path: Path,
    *,
    handler: Callable[[httpx.Request], httpx.Response],
    policy: HttpFetchPolicy | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> tuple[
    SQLiteStore,
    ResearchRepository,
    NetworkResearchRepository,
    HttpResearchService,
]:
    store, repository, network = _store(tmp_path)
    fetcher = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=fetcher,
        policy=policy,
        sleeper=sleeper or (lambda _: None),
    )
    return store, repository, network, service


def test_migration_11_applies_after_real_schema_10(tmp_path: Path) -> None:
    path = tmp_path / "v10.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(1, 11):
        for statement in MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'fixture')",
            (version,),
        )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.initialize()

    assert 11 in MIGRATIONS
    assert store.schema_version() == SCHEMA_VERSION
    with store.connection() as check:
        names = {
            row["name"]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "research_http_sources",
        "research_http_attempts",
        "research_http_snapshots",
        "corpus_http_origins",
        "research_result_sets",
        "research_result_items",
    } <= names


def test_url_credentials_and_private_targets_are_blocked_before_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"x")

    fetcher = HttpxResearchFetcher(
        resolver=lambda host, port: ("127.0.0.1",),
        transport=httpx.MockTransport(handler),
    )
    credentials = fetcher.fetch("https://user:secret@example.com/private")
    private = fetcher.fetch("https://example.com/private")
    literal = fetcher.fetch("https://127.0.0.1/private")

    assert credentials.disposition is RefreshDisposition.BLOCKED
    assert private.disposition is RefreshDisposition.BLOCKED
    assert literal.disposition is RefreshDisposition.BLOCKED
    assert requests == []


def test_redirect_to_private_target_is_revalidated_before_second_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})

    fetcher = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    result = fetcher.fetch("https://example.com/start")

    assert result.disposition is RefreshDisposition.BLOCKED
    assert result.error_code == "network_policy"
    assert len(requests) == 1
    assert requests[0].headers["host"] == "example.com"


def test_redirect_does_not_forward_set_cookie() -> None:
    observed: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.headers["host"], request.headers.get("cookie")))
        if request.headers["host"] == "example.com":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://other.example/final",
                    "Set-Cookie": "session=secret; Secure; HttpOnly",
                },
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"safe",
        )

    fetcher = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    result = fetcher.fetch("https://example.com/start")

    assert result.disposition is RefreshDisposition.CHANGED
    assert observed == [("example.com", None), ("other.example", None)]


def test_host_allowlist_and_body_limit_fail_closed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"0123456789",
        )

    fetcher = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    blocked = fetcher.fetch(
        "https://other.example/data",
        policy=HttpFetchPolicy(allowed_hosts=("example.com",)),
    )
    oversized = fetcher.fetch(
        "https://example.com/data",
        policy=HttpFetchPolicy(max_response_bytes=4),
    )

    assert blocked.disposition is RefreshDisposition.BLOCKED
    assert oversized.disposition is RefreshDisposition.FAILED
    assert oversized.error_code == "response_too_large"
    assert calls == 1


def test_etag_304_and_same_raw_200_do_not_duplicate_snapshots(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.headers.get("if-none-match") is None
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain", "ETag": '"v1"'},
                content="Український грант".encode(),
            )
        if calls == 2:
            assert request.headers["if-none-match"] == '"v1"'
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "ETag": '"v1b"'},
            content="Український грант".encode(),
        )

    _, _, network, service = _service(tmp_path, handler=handler)
    service.register_source(_source())
    first = service.refresh_source("web-1")
    second = service.refresh_source("web-1")
    third = service.refresh_source("web-1")

    assert first.disposition is RefreshDisposition.CHANGED
    assert second.disposition is RefreshDisposition.NOT_MODIFIED
    assert third.disposition is RefreshDisposition.UNCHANGED
    assert network.snapshot_count("web-1") == 1
    assert network.attempt_count("web-1") == 3
    assert network.get_source("web-1").freshness is FreshnessState.CURRENT


def test_changed_raw_bytes_can_deduplicate_to_same_normalized_document(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        body = b"alpha  beta" if calls == 1 else b"alpha beta\n"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=body,
        )

    _, repository, network, service = _service(tmp_path, handler=handler)
    service.register_source(_source())
    first = service.refresh_source("web-1")
    second = service.refresh_source("web-1")

    assert first.document_id is not None
    assert second.document_id == first.document_id
    assert first.snapshot_id != second.snapshot_id
    assert network.snapshot_count("web-1") == 2
    evidence = network.evidence_for_document(first.document_id)
    assert len(evidence) == 2
    assert all(item.source_kind is SourceKind.HTTP for item in evidence)
    assert repository.search("ws", "alpha beta")


def test_retry_after_is_bounded_and_every_attempt_is_durable(tmp_path: Path) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "99"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"ok": true}',
        )

    policy = HttpFetchPolicy(max_attempts=2, max_backoff_seconds=0.5)
    _, _, network, service = _service(
        tmp_path,
        handler=handler,
        policy=policy,
        sleeper=sleeps.append,
    )
    service.register_source(_source())
    result = service.refresh_source("web-1")

    assert result.disposition is RefreshDisposition.CHANGED
    assert result.attempts == 2
    assert network.attempt_count("web-1") == 2
    assert sleeps == [0.5]


@pytest.mark.parametrize(
    ("status", "disposition", "freshness"),
    [
        (401, RefreshDisposition.BLOCKED, FreshnessState.BLOCKED),
        (404, RefreshDisposition.REMOVED, FreshnessState.REMOVED),
    ],
)
def test_auth_and_removed_sources_have_distinct_states(
    tmp_path: Path,
    status: int,
    disposition: RefreshDisposition,
    freshness: FreshnessState,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status)

    _, _, network, service = _service(tmp_path, handler=handler)
    service.register_source(_source())
    result = service.refresh_source("web-1")

    assert result.disposition is disposition
    assert network.get_source("web-1").freshness is freshness
    assert network.snapshot_count("web-1") == 0


def test_script_only_html_requires_dynamic_fallback_without_browser_launch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html><script>renderApp()</script></html>",
        )

    _, _, network, service = _service(tmp_path, handler=handler)
    service.register_source(_source())
    result = service.refresh_source("web-1")

    assert result.disposition is RefreshDisposition.DYNAMIC_REQUIRED
    assert result.document_id is None
    assert result.snapshot_id is not None
    assert network.snapshot_count("web-1") == 1
    state = network.get_source("web-1")
    assert state.freshness is FreshnessState.CURRENT
    assert state.last_error_code == "dynamic_required"


def test_result_set_persists_local_and_http_evidence_with_freshness(tmp_path: Path) -> None:
    store, repository, network = _store(tmp_path)
    local_root = tmp_path / "local"
    local_root.mkdir()
    local_path = local_root / "same.txt"
    local_path.write_text("shared grant evidence", encoding="utf-8")
    local = LocalCorpusService(repository, allowed_root=local_root)
    local_result = local.ingest(
        SourceSpec("local-1", "ws", SourceKind.LOCAL_FILE, str(local_path))
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"shared grant evidence",
        )

    web = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    web.register_source(_source())
    remote = web.refresh_source("web-1")
    assert remote.document_id == local_result.document.document_id

    results = ResearchResultService(
        repository=repository,
        network_repository=network,
    )
    result_set = results.search("ws", "grant evidence")
    restarted = ResearchResultService(
        repository=ResearchRepository(SQLiteStore(store.path)),
        network_repository=NetworkResearchRepository(SQLiteStore(store.path)),
    ).get(result_set.result_set_id)
    rendered = results.render_text(restarted)

    assert len(restarted.items) == 1
    assert {item.source_kind for item in restarted.items[0].evidence} == {
        SourceKind.LOCAL_FILE,
        SourceKind.HTTP,
    }
    http_evidence = next(
        item for item in restarted.items[0].evidence if item.source_kind is SourceKind.HTTP
    )
    assert http_evidence.freshness is FreshnessState.CURRENT
    assert "Why matched:" in rendered
    assert "HTTP, freshness=current" in rendered
    assert str(local_path) in rendered
    assert "https://example.com/page" in rendered


class _CrashThenRepairWeb:
    def __init__(self, *, crash_source: str | None) -> None:
        self.crash_source = crash_source
        self.calls: list[str] = []

    def refresh_source(self, source_id: str, *, task_id: str | None = None) -> RefreshResult:
        assert task_id is not None
        self.calls.append(source_id)
        if source_id == self.crash_source:
            raise RuntimeError("simulated process crash")
        return RefreshResult(
            source_id=source_id,
            disposition=RefreshDisposition.CHANGED,
            attempts=1,
        )


def test_refresh_job_resumes_from_checksum_checkpoint_after_crash(tmp_path: Path) -> None:
    store, _, network = _store(tmp_path)
    network.register_source(_source("a", "https://example.com/a"))
    network.register_source(_source("b", "https://example.com/b"))
    tasks = TaskQueue(store)
    checkpoints = CheckpointService(store)
    crashing = _CrashThenRepairWeb(crash_source="b")
    first = ResearchRefreshService(
        tasks=tasks,
        checkpoints=checkpoints,
        network_repository=network,
        web=crashing,  # type: ignore[arg-type]
    )
    task_id = first.create_job(workspace_id="ws", source_ids=("a", "b"))

    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.run(task_id)
    partial = first.summary(task_id)
    assert partial.state == "running"
    assert partial.processed == 1
    assert crashing.calls == ["a", "b"]

    repaired = _CrashThenRepairWeb(crash_source=None)
    resumed = ResearchRefreshService(
        tasks=TaskQueue(SQLiteStore(store.path)),
        checkpoints=CheckpointService(SQLiteStore(store.path)),
        network_repository=NetworkResearchRepository(SQLiteStore(store.path)),
        web=repaired,  # type: ignore[arg-type]
    )
    completed = resumed.run(task_id)

    assert completed.state == "completed"
    assert completed.processed == 2
    assert repaired.calls == ["b"]


def test_refresh_job_pause_resume_and_cancel_use_canonical_task_transitions(
    tmp_path: Path,
) -> None:
    store, _, network = _store(tmp_path)
    network.register_source(_source("a", "https://example.com/a"))
    web = _CrashThenRepairWeb(crash_source=None)
    jobs = ResearchRefreshService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        network_repository=network,
        web=web,  # type: ignore[arg-type]
    )
    task_id = jobs.create_job(workspace_id="ws")

    assert jobs.pause(task_id).state == "paused"
    assert jobs.resume(task_id).state == "completed"

    second_id = jobs.create_job(workspace_id="ws")
    assert jobs.cancel(second_id).state == "cancelled"
    assert jobs.run(second_id).state == "cancelled"
