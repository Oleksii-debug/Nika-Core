from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
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


def _environment(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Formal authority",
        spec=ProductProjectSpec(
            goal="Preserve formal research authority",
            desired_outcome="Fail closed after durable evidence corruption",
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
            text="Formal evidence must retain durable authority.",
            media_type="text/plain",
        ),
    ).document
    ScopedResearchResultWriter(
        store=store,
        network_repository=network,
    ).save(
        workspace_id="ws",
        query="formal research authority",
        hits=[
            SearchHit(
                document_id=document.document_id,
                title=document.title,
                snippet="Formal evidence must retain durable authority.",
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
    return store, projects, service


def _options() -> tuple[ProductOption, ...]:
    return (
        ProductOption(
            option_id="option-1",
            title="Formal option",
            summary="Use the formal evidence package.",
            evidence_package_ids=("research-1",),
        ),
    )


def _decision() -> ProductDecision:
    return ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Approved from formal research",
        decided_by_ref="user://owner",
    )


def _handoff(service: ResearchProductHandoffService):
    return service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )


def test_formal_handoff_downgrade_cannot_hide_missing_seal(tmp_path) -> None:
    store, projects, service = _environment(tmp_path)
    _handoff(service)

    with store.connection() as conn:
        authority = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        ).fetchone()[0]
        assert authority == 1

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

    decisions = ProductDecisionRepository(store)
    with pytest.raises(ProductProjectError, match="integrity seal is missing"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:downgrade",
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0
    assert projects.get("p1").row_version == 0


def test_formal_authority_fingerprint_tampering_fails_closed(tmp_path) -> None:
    store, _, service = _environment(tmp_path)
    _handoff(service)

    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency "
            "SET input_fingerprint=? "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'",
            ("0" * 64,),
        )

    with pytest.raises(ProductProjectError, match="formal research handoff authority mismatch"):
        service.get("p1", "research-1")


def test_replay_repairs_missing_formal_authority_when_seal_is_intact(tmp_path) -> None:
    store, _, service = _environment(tmp_path)
    created = _handoff(service)

    with store.connection() as conn:
        conn.execute(
            "DELETE FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        )

    with pytest.raises(ProductProjectError, match="formal research handoff authority is missing"):
        service.get("p1", "research-1")

    assert _handoff(service) == created
    with store.connection() as conn:
        authority = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        ).fetchone()[0]
    assert authority == 1


def test_legacy_direct_handoff_remains_compatible_without_formal_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "legacy.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p-legacy",
        name="Legacy project",
        spec=ProductProjectSpec(goal="Legacy", desired_outcome="Remain compatible"),
        idempotency_key="create:p-legacy",
    )
    projects.record_research_handoff(
        "p-legacy",
        ResearchEvidencePackage(
            package_id="legacy-1",
            evidence=(
                EvidenceRef(
                    evidence_id="legacy-evidence",
                    provenance_ref="legacy://evidence/1",
                    claim="Legacy evidence",
                ),
            ),
        ),
        (
            ProductOption(
                option_id="legacy-option",
                title="Legacy option",
                summary="Existing direct ProductProject handoff",
                evidence_package_ids=("legacy-1",),
            ),
        ),
    )

    decision = ProductDecision(
        decision_id="legacy-decision",
        option_id="legacy-option",
        state=ProductDecisionState.APPROVED,
        rationale="Preserve legacy compatibility",
        decided_by_ref="user://owner",
    )
    stored = ProductDecisionRepository(store).record(
        "p-legacy",
        decision,
        expected_row_version=0,
        idempotency_key="decision:legacy",
    )
    assert stored.evidence_package_ids == ("legacy-1",)

    with store.connection() as conn:
        authority = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE project_id='p-legacy' "
            "AND operation_kind='research_product_handoff.formal_authority'"
        ).fetchone()[0]
    assert authority == 0


def test_formal_authority_commits_before_handoff_payload_for_crash_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    store, _, service = _environment(tmp_path)

    def fail_record(*args, **kwargs):
        raise RuntimeError("simulated handoff persistence crash")

    monkeypatch.setattr(service.projects, "record_research_handoff", fail_record)

    with pytest.raises(RuntimeError, match="simulated handoff persistence crash"):
        _handoff(service)

    with store.connection() as conn:
        authority = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority' "
            "AND entity_id='research-1'"
        ).fetchone()[0]
        handoff = conn.execute(
            "SELECT COUNT(*) FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()[0]
        seals = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.research_product_handoff_sealed' "
            "AND entity_type='product_project' AND entity_id='p1'"
        ).fetchone()[0]

    assert authority == 1
    assert handoff == 0
    assert seals == 0
