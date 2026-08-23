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


def _sealed_environment(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Evidence-backed product",
        spec=ProductProjectSpec(
            goal="Build the product",
            desired_outcome="Use formal research evidence",
        ),
        idempotency_key="create:p1",
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
            text="Durable product research evidence.",
            media_type="text/plain",
        ),
    ).document
    ScopedResearchResultWriter(
        store=store,
        network_repository=network,
    ).save(
        workspace_id="ws",
        query="durable product research",
        hits=[
            SearchHit(
                document_id=document.document_id,
                title=document.title,
                snippet="Durable product research evidence.",
                rank=-1.0,
            )
        ],
        why_matched="deterministic literal match",
        result_set_id="rs-1",
    )

    service = ResearchProductHandoffService(
        store=store,
        network_repository=network,
    )
    service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=(
            ProductOption(
                option_id="option-1",
                title="Evidence-backed option",
                summary="Use the sealed research result.",
                evidence_package_ids=("research-1",),
            ),
        ),
    )
    return store, projects


@pytest.mark.parametrize("tamper", ["delete", "retarget"])
def test_formal_handoff_without_matching_integrity_seal_fails_closed(
    tmp_path,
    tamper: str,
) -> None:
    store, projects = _sealed_environment(tmp_path)
    with store.connection() as conn:
        if tamper == "delete":
            conn.execute(
                "DELETE FROM audit_events "
                "WHERE event_type='product_project.research_product_handoff_sealed' "
                "AND entity_type='product_project' AND entity_id='p1'"
            )
        else:
            row = conn.execute(
                "SELECT event_id,payload_json FROM audit_events "
                "WHERE event_type='product_project.research_product_handoff_sealed' "
                "AND entity_type='product_project' AND entity_id='p1'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["package_id"] = "research-other"
            conn.execute(
                "UPDATE audit_events SET payload_json=? WHERE event_id=?",
                (json.dumps(payload), row["event_id"]),
            )

    decisions = ProductDecisionRepository(store)
    decision = ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Approved from formal research",
        decided_by_ref="user://owner",
    )
    with pytest.raises(ProductProjectError, match="integrity seal is missing"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key=f"decision:{tamper}",
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0
    assert projects.get("p1").row_version == 0
