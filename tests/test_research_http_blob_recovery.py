"""A persisted HTTP digest must not outrank the corresponding raw blob on disk."""

import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    FreshnessState,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)

_BODY = b"reproducible research source content"


def _session(
    tmp_path: Path,
    *,
    second_not_modified: bool = False,
    before_second_response: Callable[[], None] | None = None,
):
    observed: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers.get("if-none-match"))
        if second_not_modified and len(observed) == 2:
            if before_second_response is not None:
                before_second_response()
            return httpx.Response(304, headers={"ETag": '"verified"'})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "ETag": '"verified"'},
            content=_BODY,
        )

    store = SQLiteStore(tmp_path / "research.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda host, port: ("93.184.216.34",),
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda delay: None,
    )
    service.register_source(
        SourceSpec("web-1", "ws", SourceKind.HTTP, "https://example.com/page")
    )
    first = service.refresh_source("web-1")
    assert first.disposition is RefreshDisposition.CHANGED
    digest = network.get_source("web-1").current_raw_sha256
    assert digest is not None
    matches = tuple((tmp_path / "blobs").rglob(digest))
    assert len(matches) == 1
    return store, network, service, matches[0], digest, observed


def test_missing_raw_blob_is_refetched_without_validator_and_restored(
    tmp_path: Path,
) -> None:
    _, network, service, path, digest, observed = _session(tmp_path)
    path.unlink()

    second = service.refresh_source("web-1")

    assert second.disposition is RefreshDisposition.UNCHANGED
    assert observed == [None, None]
    assert path.read_bytes() == _BODY
    assert network.get_source("web-1").current_raw_sha256 == digest
    assert network.get_source("web-1").freshness is FreshnessState.CURRENT
    assert network.snapshot_count("web-1") == 1
    assert network.attempt_count("web-1") == 2


@pytest.mark.parametrize(
    "corruption", ["wrong_bytes", "wrong_metadata", "wrong_metadata_type"]
)
def test_corrupt_cached_blob_is_not_claimed_current_after_refetch(
    tmp_path: Path, corruption: str,
) -> None:
    store, network, service, path, digest, observed = _session(tmp_path)
    if corruption == "wrong_bytes":
        path.write_bytes(b"x" * len(_BODY))
    else:
        bad_path = (
            "nonexistent/artifact"
            if corruption == "wrong_metadata"
            else sqlite3.Binary(b"\\xff")
        )
        with store.connection() as conn:
            conn.execute(
                "UPDATE corpus_artifacts SET storage_relpath=? WHERE raw_sha256=?",
                (bad_path, digest),
            )

    second = service.refresh_source("web-1")

    assert second.disposition is RefreshDisposition.FAILED
    assert second.error_code == "blob_storage_failed"
    assert observed == [None, None]
    assert network.get_source("web-1").current_raw_sha256 == digest
    assert network.get_source("web-1").freshness is FreshnessState.STALE
    assert network.snapshot_count("web-1") == 1
    assert network.attempt_count("web-1") == 2


def test_lost_blob_and_unconditional_304_cannot_mark_source_current(
    tmp_path: Path,
) -> None:
    _, network, service, path, digest, observed = _session(
        tmp_path, second_not_modified=True
    )
    path.unlink()

    second = service.refresh_source("web-1")

    assert second.disposition is RefreshDisposition.FAILED
    assert second.error_code == "unexpected_not_modified"
    assert observed == [None, None]
    assert network.get_source("web-1").current_raw_sha256 == digest
    assert network.get_source("web-1").freshness is FreshnessState.STALE
    assert network.snapshot_count("web-1") == 1
    assert network.attempt_count("web-1") == 2


def test_verified_blob_and_conditional_304_remain_current(tmp_path: Path) -> None:
    _, network, service, path, digest, observed = _session(
        tmp_path, second_not_modified=True
    )

    second = service.refresh_source("web-1")

    assert second.disposition is RefreshDisposition.NOT_MODIFIED
    assert second.error_code is None
    assert observed == [None, '"verified"']
    assert path.read_bytes() == _BODY
    assert network.get_source("web-1").current_raw_sha256 == digest
    assert network.get_source("web-1").freshness is FreshnessState.CURRENT
    assert network.snapshot_count("web-1") == 1
    assert network.attempt_count("web-1") == 2


def test_blob_removed_during_conditional_304_cannot_mark_source_current(
    tmp_path: Path,
) -> None:
    blob_paths: list[Path] = []

    def remove_cached_blob() -> None:
        blob_paths[0].unlink()

    _, network, service, path, digest, observed = _session(
        tmp_path,
        second_not_modified=True,
        before_second_response=remove_cached_blob,
    )
    blob_paths.append(path)

    second = service.refresh_source("web-1")

    assert second.disposition is RefreshDisposition.FAILED
    assert second.error_code == "cached_blob_changed_during_refresh"
    assert observed == [None, '"verified"']
    assert not path.exists()
    assert network.get_source("web-1").current_raw_sha256 == digest
    assert network.get_source("web-1").freshness is FreshnessState.STALE
    assert network.snapshot_count("web-1") == 1
    assert network.attempt_count("web-1") == 2


@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "https://example.com/another-page",
])
def test_http_source_identity_cannot_be_reassigned_across_workspaces(
    tmp_path: Path, url: str,
) -> None:
    store, network, _, _, digest, _ = _session(tmp_path)
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("other", "Other workspace"))
    original = network.get_source("web-1")
    assert original.workspace_id == "ws"

    with pytest.raises(ValueError, match="different workspace"):
        network.register_source(SourceSpec("web-1", "other", SourceKind.HTTP, url))

    state = network.get_source("web-1")
    assert state == original
    assert state.current_raw_sha256 == digest
    assert network.snapshot_count("web-1") == 1


def test_same_workspace_http_reregistration_preserves_cached_identity(
    tmp_path: Path,
) -> None:
    _, network, _, _, digest, _ = _session(tmp_path)
    source = SourceSpec("web-1", "ws", SourceKind.HTTP, "https://example.com/page")

    state = network.register_source(source)

    assert state.workspace_id == "ws"
    assert state.current_raw_sha256 == digest
    assert state.freshness is FreshnessState.CURRENT
    assert network.snapshot_count("web-1") == 1


def test_http_service_rejects_split_sqlite_authority_before_network_effect(
    tmp_path: Path,
) -> None:
    repository_store = SQLiteStore(tmp_path / "repository.db")
    network_store = SQLiteStore(tmp_path / "network.db")
    repository_store.initialize()
    network_store.initialize()

    def forbidden_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("constructor must fail before any network request")

    with pytest.raises(ValueError, match="same SQLite store"):
        HttpResearchService(
            repository=ResearchRepository(repository_store),
            network_repository=NetworkResearchRepository(network_store),
            blob_store=ContentAddressedBlobStore(tmp_path / "blobs-split"),
            fetcher=HttpxResearchFetcher(
                resolver=lambda host, port: ("93.184.216.34",),
                transport=httpx.MockTransport(forbidden_handler),
            ),
        )


def test_http_service_allows_separate_handles_for_same_sqlite_path(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared.db"
    SQLiteStore(database_path).initialize()
    repository = ResearchRepository(SQLiteStore(database_path))
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(SQLiteStore(database_path))

    def forbidden_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("source registration must not perform a network request")

    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs-shared"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda host, port: ("93.184.216.34",),
            transport=httpx.MockTransport(forbidden_handler),
        ),
    )
    state = service.register_source(
        SourceSpec("web-shared", "ws", SourceKind.HTTP, "https://example.com/shared")
    )

    assert state.workspace_id == "ws"
    assert state.freshness is FreshnessState.UNKNOWN
