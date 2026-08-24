from __future__ import annotations

import json

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
    ProductRequirement,
)
from nika_core.research.models import (
    ExtractedDocument,
    ResearchWorkspace,
    SearchHit,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.query_results import ScopedResearchResultWriter
from nika_core.research.repository import ResearchRepository
from nika_core.research_product_handoff import ResearchProductHandoffService


def test_same_url_content_change_invalidates_product_decision_replay(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    ProductProjectRepository(store).create(
        project_id="p1",
        name="Evidence product",
        spec=ProductProjectSpec(
            goal="Use current evidence",
            desired_outcome="Decision remains bound to exact source content",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Use evidence",
                    ("Evidence is current",),
                ),
            ),
        ),
        idempotency_key="create:p1",
    )

    research = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    research.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    source = SourceSpec(
        source_id="local-fixture",
        workspace_id="ws",
        kind=SourceKind.LOCAL_FILE,
        locator="fixture.txt",
    )
    research.upsert_source(source)
    document = research.ingest_document(
        source,
        ExtractedDocument(
            title="Evidence",
            text="Exact source content must remain current.",
            media_type="text/plain",
        ),
    ).document
    ScopedResearchResultWriter(
        store=store,
        network_repository=network,
    ).save(
        workspace_id="ws",
        query="exact source content",
        hits=[
            SearchHit(
                document_id=document.document_id,
                title=document.title,
                snippet="Exact source content must remain current.",
                rank=-1.0,
            )
        ],
        why_matched="deterministic literal match",
        result_set_id="rs-1",
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT evidence_json FROM research_result_items "
            "WHERE result_set_id='rs-1' AND ordinal=0"
        ).fetchone()
        evidence = json.loads(row["evidence_json"])
        evidence[0].update(
            {
                "source_id": "http-1",
                "source_kind": "http",
                "locator": "https://example.com/research",
                "freshness": "current",
            }
        )
        conn.execute(
            "UPDATE research_result_items SET evidence_json=? "
            "WHERE result_set_id='rs-1' AND ordinal=0",
            (json.dumps(evidence),),
        )
        conn.execute(
            "INSERT INTO research_http_sources("
            "source_id,workspace_id,url,current_raw_sha256,freshness,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                "http-1",
                "ws",
                "https://example.com/research",
                "a" * 64,
                "current",
                "2026-08-23T00:00:00+00:00",
                "2026-08-23T00:00:00+00:00",
            ),
        )

    handoff = ResearchProductHandoffService(
        store=store,
        network_repository=network,
    )
    handoff.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=(
            ProductOption(
                option_id="option-1",
                title="Current evidence option",
                summary="Use the sealed research result.",
                evidence_package_ids=("research-1",),
            ),
        ),
    )

    decision = ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Supported by current sealed evidence",
        decided_by_ref="user://owner",
    )
    decisions = ProductDecisionRepository(store)
    first = decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:approve",
    )
    assert first.decision.state is ProductDecisionState.APPROVED

    with store.connection() as conn:
        conn.execute(
            "UPDATE research_http_sources SET current_raw_sha256=?, freshness='current' "
            "WHERE source_id='http-1'",
            ("b" * 64,),
        )
        changed = conn.execute(
            "SELECT current_raw_sha256,freshness FROM research_http_sources "
            "WHERE source_id='http-1'"
        ).fetchone()
    assert changed["current_raw_sha256"] == "b" * 64
    assert changed["freshness"] == "current"

    with pytest.raises(
        ProductProjectError,
        match="source|content|snapshot|stale|research",
    ):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:approve",
        )
