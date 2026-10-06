from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpFetchPolicy,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    PaginatedResearchRefreshService,
    PaginationPolicy,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
    discover_html_pagination,
)

PUBLIC_IP = "93.184.216.34"


def _resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return (PUBLIC_IP,)


def _stack(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[
    SQLiteStore,
    NetworkResearchRepository,
    HttpResearchService,
    PaginatedResearchRefreshService,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    fetcher = HttpxResearchFetcher(
        resolver=_resolver,
        transport=httpx.MockTransport(handler),
    )
    web = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=fetcher,
        policy=HttpFetchPolicy(),
        sleeper=lambda _: None,
    )
    paginated = PaginatedResearchRefreshService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        network_repository=network,
        web=web,
    )
    return store, network, web, paginated


def _register_root(web: HttpResearchService) -> None:
    web.register_source(
        SourceSpec("root", "ws", SourceKind.HTTP, "https://example.com/page")
    )


def _extraction_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM corpus_extractions").fetchone()
    return int(row[0])


def test_same_origin_treats_explicit_default_https_port_as_equivalent() -> None:
    discovery = discover_html_pagination(
        "https://example.com/start",
        '<link rel="next" href="https://example.com:443/page2">',
    )

    assert discovery.next_urls == ("https://example.com:443/page2",)


def test_paginated_refresh_reuses_etags_and_zero_reextracts_unchanged_pages(
    tmp_path: Path,
) -> None:
    request_headers: list[tuple[str, str | None]] = []

    bodies = {
        "/page": b'<html><body>Page one<link rel="next" href="/page2"></body></html>',
        "/page2": (
            b'<html><body>Page two<link rel="next" '
            b'href="https://example.com:443/page3#section"></body></html>'
        ),
        "/page3": b"<html><body>Page three</body></html>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        request_headers.append((path, request.headers.get("if-none-match")))
        etag = f'"{path}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8", "ETag": etag},
            content=bodies[path],
        )

    store, network, web, paginated = _stack(tmp_path, handler)
    _register_root(web)

    first_task = paginated.create_job(root_source_id="root")
    first = paginated.run(first_task)

    assert first.state == "completed"
    assert first.processed == 3
    assert first.total == 3
    assert first.changed == 3
    assert first.unchanged == 0
    assert first.failed == 0
    assert len(network.list_sources("ws")) == 3
    extraction_count = _extraction_count(store)
    assert extraction_count == 3

    second_task = paginated.create_job(root_source_id="root")
    second = paginated.run(second_task)

    assert second.state == "completed"
    assert second.processed == 3
    assert second.total == 3
    assert second.changed == 0
    assert second.unchanged == 3
    assert second.failed == 0
    assert _extraction_count(store) == extraction_count
    assert request_headers == [
        ("/page", None),
        ("/page2", None),
        ("/page3", None),
        ("/page", '"/page"'),
        ("/page2", '"/page2"'),
        ("/page3", '"/page3"'),
    ]


def test_paginated_refresh_is_bounded_and_rejects_cross_origin_discovery(
    tmp_path: Path,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested.append(path)
        if path == "/page":
            body = (
                b'<html><body>One<link rel="next" href="/page2">'
                b'<a rel="next" href="https://other.example/escape">escape</a></body></html>'
            )
        elif path == "/page2":
            body = b'<html><body>Two<link rel="next" href="/page3"></body></html>'
        else:
            body = b"<html><body>Unexpected</body></html>"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=body,
        )

    _, network, web, paginated = _stack(tmp_path, handler)
    _register_root(web)
    task_id = paginated.create_job(
        root_source_id="root",
        policy=PaginationPolicy(max_pages=2),
    )

    summary = paginated.run(task_id)

    assert summary.state == "completed"
    assert summary.total == 2
    assert summary.processed == 2
    assert requested == ["/page", "/page2"]
    sources = network.list_sources("ws")
    assert len(sources) == 2
    assert all("other.example" not in source.url for source in sources)


class _CrashBeforeSecondRefresh:
    def __init__(self, delegate: HttpResearchService) -> None:
        self._delegate = delegate
        self._calls = 0
        self._blobs = delegate._blobs

    def register_source(self, source: SourceSpec):
        return self._delegate.register_source(source)

    def refresh_source(self, source_id: str, *, task_id: str | None = None):
        self._calls += 1
        if self._calls == 2:
            raise RuntimeError("simulated process crash before second page fetch")
        return self._delegate.refresh_source(source_id, task_id=task_id)


def test_paginated_refresh_resumes_from_checkpoint_without_refetching_completed_page(
    tmp_path: Path,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/page":
            body = b'<html><body>One<link rel="next" href="/page2"></body></html>'
        else:
            body = b"<html><body>Two</body></html>"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=body,
        )

    store, network, web, paginated = _stack(tmp_path, handler)
    _register_root(web)
    task_id = paginated.create_job(root_source_id="root")
    crashing = PaginatedResearchRefreshService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        network_repository=network,
        web=_CrashBeforeSecondRefresh(web),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        crashing.run(task_id)

    after_crash = paginated.summary(task_id)
    assert after_crash.state == "running"
    assert after_crash.processed == 1
    assert after_crash.total == 2
    assert requested == ["/page"]

    restarted = PaginatedResearchRefreshService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        network_repository=NetworkResearchRepository(store),
        web=web,
    )
    completed = restarted.run(task_id)

    assert completed.state == "completed"
    assert completed.processed == 2
    assert completed.total == 2
    assert requested == ["/page", "/page2"]
