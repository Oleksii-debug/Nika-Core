from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.source_identity import ResearchSourceIdentityError

_PUBLIC_IP = "93.184.216.34"
_CANARY = "nika_slot3_http_origin_secret_canary"
_PAYLOADS = {
    "/alpha": b"alpha workspace research evidence",
    "/beta": b"beta workspace research evidence",
    "/gamma": b"gamma same-workspace research evidence",
}


def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return (_PUBLIC_IP,)


def _system(
    tmp_path: Path,
) -> tuple[SQLiteStore, NetworkResearchRepository, HttpResearchService]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws-a", "Workspace A"))
    repository.upsert_workspace(ResearchWorkspace("ws-b", "Workspace B"))
    network = NetworkResearchRepository(store)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _PAYLOADS[request.url.path]
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/plain"},
            content=payload,
        )

    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    return store, network, service


def _ingest(
    service: HttpResearchService,
    *,
    source_id: str,
    workspace_id: str,
    path: str,
) -> tuple[str, str]:
    service.register_source(
        SourceSpec(
            source_id,
            workspace_id,
            SourceKind.HTTP,
            f"https://example.com{path}",
        )
    )
    refreshed = service.refresh_source(source_id)
    assert refreshed.disposition is RefreshDisposition.CHANGED
    assert refreshed.document_id is not None
    assert refreshed.snapshot_id is not None
    return refreshed.document_id, refreshed.snapshot_id


def test_http_origin_rejects_cross_workspace_document_source_snapshot_mix(
    tmp_path: Path,
) -> None:
    store, network, service = _system(tmp_path)
    document_a, _snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )
    _document_b, snapshot_b = _ingest(
        service,
        source_id="source-b",
        workspace_id="ws-b",
        path="/beta",
    )

    try:
        network.link_document_origin(
            document_id=document_a,
            source_id="source-b",
            snapshot_id=snapshot_b,
            locator="https://example.com/beta",
        )
    except (ResearchSourceIdentityError, ValueError, sqlite3.IntegrityError):
        pass

    with store.connection() as conn:
        mixed = conn.execute(
            """SELECT 1 FROM corpus_http_origins
            WHERE document_id=? AND source_id=? AND snapshot_id=?""",
            (document_a, "source-b", snapshot_b),
        ).fetchone()
    assert mixed is None


def test_http_origin_rejects_snapshot_from_different_source_same_workspace(
    tmp_path: Path,
) -> None:
    store, network, service = _system(tmp_path)
    document_a, _snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )
    _document_c, snapshot_c = _ingest(
        service,
        source_id="source-c",
        workspace_id="ws-a",
        path="/gamma",
    )

    try:
        network.link_document_origin(
            document_id=document_a,
            source_id="source-a",
            snapshot_id=snapshot_c,
            locator="https://example.com/alpha",
        )
    except (ResearchSourceIdentityError, ValueError, sqlite3.IntegrityError):
        pass

    with store.connection() as conn:
        mismatched = conn.execute(
            """SELECT 1 FROM corpus_http_origins
            WHERE document_id=? AND source_id=? AND snapshot_id=?""",
            (document_a, "source-a", snapshot_c),
        ).fetchone()
    assert mismatched is None


def test_corrupt_http_origin_locator_does_not_escape_after_restart(tmp_path: Path) -> None:
    store, _network, service = _system(tmp_path)
    document_a, snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )
    credential_locator = f"https://example.com/alpha?access_token={_CANARY}"

    with store.connection() as conn:
        conn.execute(
            """UPDATE corpus_http_origins SET locator=?
            WHERE document_id=? AND source_id=? AND snapshot_id=?""",
            (credential_locator, document_a, "source-a", snapshot_a),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    try:
        evidence = restarted.evidence_for_document(document_a)
    except ResearchSourceIdentityError as exc:
        assert _CANARY not in str(exc)
        assert _CANARY not in repr(exc)
    else:
        assert _CANARY not in repr(evidence)


def test_valid_http_origin_remains_readable_across_restart(tmp_path: Path) -> None:
    store, _network, service = _system(tmp_path)
    document_a, _snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    evidence = restarted.evidence_for_document(document_a)

    assert len(evidence) == 1
    assert evidence[0].source_id == "source-a"
    assert evidence[0].locator == "https://example.com/alpha"
