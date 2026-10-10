from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

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
_SECRET = "nika_origin_integrity_canary"
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
) -> tuple[
    SQLiteStore,
    ResearchRepository,
    NetworkResearchRepository,
    HttpResearchService,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws-a", "Workspace A"))
    repository.upsert_workspace(ResearchWorkspace("ws-b", "Workspace B"))
    network = NetworkResearchRepository(store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/plain"},
            content=_PAYLOADS[request.url.path],
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
    return store, repository, network, service


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


def test_http_origin_rejects_cross_workspace_chain_before_insert(tmp_path: Path) -> None:
    store, _repository, network, service = _system(tmp_path)
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

    with pytest.raises(ResearchSourceIdentityError) as rejected:
        network.link_document_origin(
            document_id=document_a,
            source_id="source-b",
            snapshot_id=snapshot_b,
            locator="https://example.com/beta",
        )

    assert rejected.value.code == "origin_identity_conflict"
    with store.connection() as conn:
        mixed = conn.execute(
            """SELECT 1 FROM corpus_http_origins
            WHERE document_id=? AND source_id=? AND snapshot_id=?""",
            (document_a, "source-b", snapshot_b),
        ).fetchone()
    assert mixed is None


def test_http_origin_rejects_snapshot_owned_by_another_source(tmp_path: Path) -> None:
    store, _repository, network, service = _system(tmp_path)
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

    with pytest.raises(ResearchSourceIdentityError) as rejected:
        network.link_document_origin(
            document_id=document_a,
            source_id="source-a",
            snapshot_id=snapshot_c,
            locator="https://example.com/alpha",
        )

    assert rejected.value.code == "origin_identity_conflict"
    with store.connection() as conn:
        mismatched = conn.execute(
            """SELECT 1 FROM corpus_http_origins
            WHERE document_id=? AND source_id=? AND snapshot_id=?""",
            (document_a, "source-a", snapshot_c),
        ).fetchone()
    assert mismatched is None


def test_restart_fails_closed_without_echoing_corrupt_http_origin_locator(
    tmp_path: Path,
) -> None:
    store, _repository, _network, service = _system(tmp_path)
    document_a, snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )
    credential_locator = f"https://example.com/alpha?access_token={_SECRET}"

    with store.connection() as conn:
        conn.execute(
            """UPDATE corpus_http_origins SET locator=?
            WHERE document_id=? AND source_id=? AND snapshot_id=?""",
            (credential_locator, document_a, "source-a", snapshot_a),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    with pytest.raises(ResearchSourceIdentityError) as rejected:
        restarted.evidence_for_document(document_a)

    assert rejected.value.code == "origin_identity_corrupt"
    assert _SECRET not in str(rejected.value)
    assert _SECRET not in repr(rejected.value)


def test_valid_http_origin_is_canonical_and_stable_across_restart(tmp_path: Path) -> None:
    store, _repository, network, service = _system(tmp_path)
    document_a, snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )

    network.link_document_origin(
        document_id=document_a,
        source_id="source-a",
        snapshot_id=snapshot_a,
        locator="HTTPS://EXAMPLE.COM:443/alpha#fragment",
    )
    with pytest.raises(ResearchSourceIdentityError) as rebound:
        network.link_document_origin(
            document_id=document_a,
            source_id="source-a",
            snapshot_id=snapshot_a,
            locator="https://example.com/other",
        )
    assert rebound.value.code == "origin_locator_conflict"

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    evidence = restarted.evidence_for_document(document_a)
    assert len(evidence) == 1
    assert evidence[0].source_id == "source-a"
    assert evidence[0].locator == "https://example.com/alpha"


def test_persisted_result_set_does_not_replay_credential_origin_after_restart(
    tmp_path: Path,
) -> None:
    store, repository, network, service = _system(tmp_path)
    _document_a, _snapshot_a = _ingest(
        service,
        source_id="source-a",
        workspace_id="ws-a",
        path="/alpha",
    )
    hits = repository.search("ws-a", "alpha")
    saved = network.save_result_set(workspace_id="ws-a", query="alpha", hits=hits)
    assert saved.items

    with store.connection() as conn:
        row = conn.execute(
            """SELECT evidence_json FROM research_result_items
            WHERE result_set_id=? AND ordinal=0""",
            (saved.result_set_id,),
        ).fetchone()
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        evidence[0]["locator"] = f"https://example.com/alpha?api_key={_SECRET}"
        conn.execute(
            """UPDATE research_result_items SET evidence_json=?
            WHERE result_set_id=? AND ordinal=0""",
            (json.dumps(evidence), saved.result_set_id),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    with pytest.raises(ResearchSourceIdentityError) as rejected:
        restarted.get_result_set(saved.result_set_id)

    assert rejected.value.code == "origin_identity_corrupt"
    assert _SECRET not in str(rejected.value)
    assert _SECRET not in repr(rejected.value)