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


def _environment(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Accessible product",
        spec=ProductProjectSpec(
            goal="Build an accessible product",
            desired_outcome="Evidence-backed requirements",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Keyboard operation",
                    ("All primary actions are keyboard reachable",),
                ),
            ),
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
        locator="notes.txt",
    )
    research.upsert_source(source)
    document = research.ingest_document(
        source,
        ExtractedDocument(
            title="Accessibility evidence",
            text="Keyboard operation requires deterministic focus.",
            media_type="text/plain",
        ),
    ).document
    result_set = ScopedResearchResultWriter(
        store=store,
        network_repository=network,
    ).save(
        workspace_id="ws",
        query="keyboard accessibility",
        hits=[
            SearchHit(
                document_id=document.document_id,
                title=document.title,
                snippet="Keyboard operation requires deterministic focus.",
                rank=-1.0,
            )
        ],
        why_matched="deterministic literal match",
        result_set_id="rs-1",
    )
    assert result_set.items[0].evidence

    service = ResearchProductHandoffService(
        store=store,
        network_repository=network,
    )
    return store, projects, network, service


def _options(package_id: str = "research-1") -> tuple[ProductOption, ...]:
    return (
        ProductOption(
            option_id="option-1",
            title="Semantic keyboard path",
            summary="Use deterministic semantic controls before visual fallback.",
            evidence_package_ids=(package_id,),
        ),
    )


def _approved_decision() -> ProductDecision:
    return ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Best supported option",
        decided_by_ref="user://owner",
    )


def test_canonical_research_handoff_is_sealed_restart_safe_and_structured(tmp_path) -> None:
    store, _, _, service = _environment(tmp_path)

    created = service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )
    replay = service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )

    assert replay == created
    assert created.option_ids == ("option-1",)
    assert len(created.result_set_sha256) == 64
    assert len(created.handoff_payload_sha256) == 64
    assert created.to_dict()["result_set_id"] == "rs-1"

    with store.connection() as conn:
        handoff = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()
        seals = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.research_product_handoff_sealed' "
            "AND entity_type='product_project' AND entity_id='p1'"
        ).fetchone()[0]
    payload = json.loads(handoff["payload_json"])
    assert payload["research_artifact_ref"] == "research-result-set://ws/rs-1"
    assert payload["evidence"][0]["provenance_ref"].startswith(
        "research-result-set://ws/rs-1/items/0/evidence/"
    )
    assert "notes.txt" not in handoff["payload_json"]
    assert seals == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ResearchProductHandoffService(
        store=restarted_store,
        network_repository=NetworkResearchRepository(restarted_store),
    )
    assert restarted.get("p1", "research-1") == created


def test_formal_handoff_rejects_stale_remote_evidence(tmp_path) -> None:
    store, projects, _, service = _environment(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT evidence_json FROM research_result_items "
            "WHERE result_set_id='rs-1' AND ordinal=0"
        ).fetchone()
        evidence = json.loads(row["evidence_json"])
        evidence[0]["source_kind"] = "http"
        evidence[0]["freshness"] = "stale"
        conn.execute(
            "UPDATE research_result_items SET evidence_json=? "
            "WHERE result_set_id='rs-1' AND ordinal=0",
            (json.dumps(evidence),),
        )

    with pytest.raises(
        ProductProjectError,
        match="remote research evidence must be current",
    ):
        service.handoff(
            project_id="p1",
            result_set_id="rs-1",
            package_id="research-1",
            options=_options(),
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM product_research_handoffs WHERE project_id='p1'"
        ).fetchone()[0]
    assert count == 0
    assert projects.get("p1").row_version == 0


def test_sealed_handoff_tampering_fails_before_product_decision(tmp_path) -> None:
    store, projects, _, service = _environment(tmp_path)
    service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["options"][0]["summary"] = "tampered summary"
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id='p1' AND package_id='research-1'",
            (json.dumps(payload),),
        )

    decisions = ProductDecisionRepository(store)
    with pytest.raises(ProductProjectError, match="research handoff integrity mismatch"):
        decisions.record(
            "p1",
            _approved_decision(),
            expected_row_version=0,
            idempotency_key="decision:approve",
        )

    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0]
    assert count == 0
    assert projects.get("p1").row_version == 0


def test_result_set_tampering_is_detected_after_formal_handoff(tmp_path) -> None:
    store, _, _, service = _environment(tmp_path)
    service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE research_result_sets SET query='tampered query' "
            "WHERE result_set_id='rs-1'"
        )

    with pytest.raises(ProductProjectError, match="research result integrity mismatch"):
        service.get("p1", "research-1")


def test_requirement_inherits_approved_decision_evidence_and_survives_restart(tmp_path) -> None:
    store, _, _, service = _environment(tmp_path)
    service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )
    decisions = ProductDecisionRepository(store)
    decisions.record(
        "p1",
        _approved_decision(),
        expected_row_version=0,
        idempotency_key="decision:approve",
    )

    linked = decisions.link_requirement(
        "p1",
        requirement_id="req-1",
        decision_id="decision-1",
        expected_row_version=1,
    )
    requirement = linked.spec.requirements[0]
    assert requirement.decision_ids == ("decision-1",)
    assert requirement.evidence_package_ids == ("research-1",)
    assert linked.spec_version == 2
    assert linked.row_version == 2

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    recovered = ProductProjectRepository(restarted_store).get("p1")
    recovered_requirement = recovered.spec.requirements[0]
    assert recovered_requirement.decision_ids == ("decision-1",)
    assert recovered_requirement.evidence_package_ids == ("research-1",)


def test_decision_rejects_formal_handoff_when_remote_source_becomes_stale(tmp_path) -> None:
    store, projects, _, service = _environment(tmp_path)
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
            "source_id,workspace_id,url,freshness,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?)",
            (
                "http-1",
                "ws",
                "https://example.com/research",
                "current",
                "2026-08-23T00:00:00+00:00",
                "2026-08-23T00:00:00+00:00",
            ),
        )

    service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE research_http_sources SET freshness='stale' WHERE source_id='http-1'"
        )

    decisions = ProductDecisionRepository(store)
    with pytest.raises(ProductProjectError, match="remote research source is not current"):
        decisions.record(
            "p1",
            _approved_decision(),
            expected_row_version=0,
            idempotency_key="decision:approve:stale-source",
        )
    assert projects.get("p1").row_version == 0


def test_conflicting_handoff_replay_fails_closed(tmp_path) -> None:
    _, _, _, service = _environment(tmp_path)
    service.handoff(
        project_id="p1",
        result_set_id="rs-1",
        package_id="research-1",
        options=_options(),
    )
    changed = (
        ProductOption(
            option_id="option-1",
            title="Changed title",
            summary="Same identity but different product meaning.",
            evidence_package_ids=("research-1",),
        ),
    )

    with pytest.raises(ProductProjectError, match="different handoff payload"):
        service.handoff(
            project_id="p1",
            result_set_id="rs-1",
            package_id="research-1",
            options=changed,
        )
