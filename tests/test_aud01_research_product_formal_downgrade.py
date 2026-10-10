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


def test_sealed_handoff_cannot_be_downgraded_to_legacy_by_mutating_same_store(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p-aud01",
        name="AUD01 provenance downgrade",
        spec=ProductProjectSpec(
            goal="Preserve formal research authority",
            desired_outcome="A sealed handoff cannot become legacy after corruption",
        ),
        idempotency_key="create:p-aud01",
    )

    research = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    research.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    source = SourceSpec(
        source_id="local-1",
        workspace_id="ws",
        kind=SourceKind.LOCAL_FILE,
        locator="evidence.txt",
    )
    research.upsert_source(source)
    document = research.ingest_document(
        source,
        ExtractedDocument(
            title="Evidence",
            text="Formal research evidence.",
            media_type="text/plain",
        ),
    ).document
    ScopedResearchResultWriter(store=store, network_repository=network).save(
        workspace_id="ws",
        query="formal evidence",
        hits=[
            SearchHit(
                document_id=document.document_id,
                title=document.title,
                snippet="Formal research evidence.",
                rank=-1.0,
            )
        ],
        why_matched="deterministic literal match",
        result_set_id="rs-aud01",
    )
    ResearchProductHandoffService(store=store, network_repository=network).handoff(
        project_id="p-aud01",
        result_set_id="rs-aud01",
        package_id="research-aud01",
        options=(
            ProductOption(
                option_id="option-aud01",
                title="Formal option",
                summary="Use formal evidence.",
                evidence_package_ids=("research-aud01",),
            ),
        ),
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id=? AND package_id=?",
            ("p-aud01", "research-aud01"),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["research_artifact_ref"] = "legacy-artifact"
        for evidence in payload["evidence"]:
            evidence["provenance_ref"] = "legacy://rewritten"
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id=? AND package_id=?",
            (json.dumps(payload), "p-aud01", "research-aud01"),
        )
        conn.execute(
            "DELETE FROM audit_events "
            "WHERE event_type='product_project.research_product_handoff_sealed' "
            "AND entity_type='product_project' AND entity_id=?",
            ("p-aud01",),
        )

    decision = ProductDecision(
        decision_id="decision-aud01",
        option_id="option-aud01",
        state=ProductDecisionState.APPROVED,
        rationale="Must retain formal provenance authority",
        decided_by_ref="user://owner",
    )
    with pytest.raises(ProductProjectError):
        ProductDecisionRepository(store).record(
            "p-aud01",
            decision,
            expected_row_version=0,
            idempotency_key="decision:aud01",
        )
