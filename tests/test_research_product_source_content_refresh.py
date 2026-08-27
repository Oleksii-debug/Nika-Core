from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchResultService,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research_product_handoff import ResearchProductHandoffService

_PUBLIC_IP = "93.184.216.34"


def _environment(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    research = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    research.upsert_workspace(ResearchWorkspace("ws-a", "Research A"))

    response_state = {
        "body": b"keyboard operation needs deterministic semantic focus evidence",
        "etag": '"v1"',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/plain", "ETag": str(response_state["etag"])},
            content=bytes(response_state["body"]),
        )

    research_service = HttpResearchService(
        repository=research,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda _host, _port: (_PUBLIC_IP,),
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    research_service.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "https://example.com/research#ignored-fragment",
        )
    )
    first = research_service.refresh_source("source-a")
    assert first.disposition is RefreshDisposition.CHANGED

    result_set = ResearchResultService(
        repository=research,
        network_repository=network,
    ).search("ws-a", "deterministic semantic focus")
    assert result_set.items
    assert result_set.items[0].evidence

    ProductProjectRepository(store).create(
        project_id="p1",
        name="Accessible evidence-backed product",
        spec=ProductProjectSpec(
            goal="Build an accessible product",
            desired_outcome="Requirements preserve exact research provenance",
        ),
        idempotency_key="create:p1",
    )
    handoff = ResearchProductHandoffService(store=store, network_repository=network)
    option = ProductOption(
        option_id="option-1",
        title="Semantic keyboard path",
        summary="Use deterministic semantic focus backed by research.",
        evidence_package_ids=("research-evidence-1",),
    )
    handoff.handoff(
        project_id="p1",
        result_set_id=result_set.result_set_id,
        package_id="research-evidence-1",
        options=(option,),
    )
    decision = ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="The option is supported by sealed research evidence.",
        decided_by_ref="user://owner",
    )
    decisions = ProductDecisionRepository(store)
    decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:approve",
    )
    return store, network, research_service, response_state, handoff, decisions, decision


def test_actual_http_refresh_invalidates_old_formal_handoff_and_decision_replay(
    tmp_path: Path,
) -> None:
    (
        store,
        network,
        research_service,
        response_state,
        handoff,
        decisions,
        decision,
    ) = _environment(tmp_path)
    original_sha = network.get_source("source-a").current_raw_sha256
    assert original_sha is not None

    with store.connection() as conn:
        seal = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE event_type='product_project.research_product_handoff_sealed' "
            "AND entity_id='p1'"
        ).fetchone()["payload_json"]
    seal_payload = json.loads(seal)
    assert seal_payload["seal_version"] == 2
    assert seal_payload["source_content_bindings"]
    assert seal_payload["source_content_bindings"][0]["raw_sha256"] == original_sha
    assert "https://example.com" not in seal

    response_state["body"] = b"changed source bytes with replacement research evidence"
    response_state["etag"] = '"v2"'
    changed = research_service.refresh_source("source-a")
    assert changed.disposition is RefreshDisposition.CHANGED
    current_sha = network.get_source("source-a").current_raw_sha256
    assert current_sha is not None
    assert current_sha != original_sha

    with pytest.raises(ProductProjectError, match="source content changed"):
        handoff.get("p1", "research-evidence-1")
    with pytest.raises(ProductProjectError, match="source content changed"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:approve",
        )

    project = ProductProjectRepository(store).get("p1")
    assert project.row_version == 1
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 1


def test_formal_handoff_rejects_real_ordinal_alias_after_seal(
    tmp_path: Path,
) -> None:
    store, _network, _service, _state, handoff, decisions, decision = _environment(tmp_path)

    with store.connection() as conn:
        result_set_id = conn.execute(
            "SELECT result_set_id FROM research_result_sets"
        ).fetchone()["result_set_id"]
        conn.execute(
            "UPDATE research_result_items SET ordinal=? "
            "WHERE result_set_id=? AND ordinal=0",
            (0.5, result_set_id),
        )
        corrupted = conn.execute(
            "SELECT ordinal,typeof(ordinal) AS storage_type "
            "FROM research_result_items WHERE result_set_id=?",
            (result_set_id,),
        ).fetchone()
        assert corrupted["ordinal"] == 0.5
        assert corrupted["storage_type"] == "real"

    with pytest.raises(ProductProjectError, match="durable ordinal"):
        handoff.get("p1", "research-evidence-1")
    with pytest.raises(ProductProjectError, match="durable ordinal"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:approve",
        )

    project = ProductProjectRepository(store).get("p1")
    assert project.row_version == 1
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 1
