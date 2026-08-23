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


def _formal_handoff(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p1",
        name="Formal authority lookup",
        spec=ProductProjectSpec(
            goal="Preserve formal authority",
            desired_outcome="Metadata corruption cannot downgrade formal research",
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
            text="Formal evidence must remain formal after corruption.",
            media_type="text/plain",
        ),
    ).document
    ScopedResearchResultWriter(store=store, network_repository=network).save(
        workspace_id="ws",
        query="formal authority",
        hits=[
            SearchHit(
                document_id=document.document_id,
                title=document.title,
                snippet="Formal evidence must remain formal after corruption.",
                rank=-1.0,
            )
        ],
        why_matched="deterministic literal match",
        result_set_id="rs-1",
    )
    ResearchProductHandoffService(store=store, network_repository=network).handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=(
            ProductOption(
                option_id="option-1",
                title="Formal option",
                summary="Use formal evidence.",
                evidence_package_ids=("research-1",),
            ),
        ),
    )
    return store


def _downgrade_payload_and_remove_seal(store: SQLiteStore) -> None:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["research_artifact_ref"] = "legacy-artifact"
        for evidence in payload["evidence"]:
            evidence["provenance_ref"] = "legacy://rewritten"
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id='p1' AND package_id='research-1'",
            (json.dumps(payload),),
        )
        conn.execute(
            "DELETE FROM audit_events "
            "WHERE event_type='product_project.research_product_handoff_sealed' "
            "AND entity_type='product_project' AND entity_id='p1'"
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    (
        ("operation_key", "research-product-formal:corrupt"),
        ("operation_kind", "research_product_handoff.legacy"),
        ("entity_id", "legacy-package"),
    ),
)
def test_formal_authority_identity_corruption_cannot_hide_marker(
    tmp_path,
    column: str,
    replacement: str,
) -> None:
    store = _formal_handoff(tmp_path)
    _downgrade_payload_and_remove_seal(store)

    update_sql = {
        "operation_key": (
            "UPDATE product_project_mutation_idempotency SET operation_key=? "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        ),
        "operation_kind": (
            "UPDATE product_project_mutation_idempotency SET operation_kind=? "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        ),
        "entity_id": (
            "UPDATE product_project_mutation_idempotency SET entity_id=? "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        ),
    }[column]
    with store.connection() as conn:
        conn.execute(update_sql, (replacement,))

    decision = ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Formal evidence remains required",
        decided_by_ref="user://owner",
    )
    with pytest.raises(ProductProjectError, match="formal research handoff authority is malformed"):
        ProductDecisionRepository(store).record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:formal-authority-lookup",
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0
        row_version = conn.execute(
            "SELECT row_version FROM product_projects WHERE project_id='p1'"
        ).fetchone()[0]
    assert row_version == 0
