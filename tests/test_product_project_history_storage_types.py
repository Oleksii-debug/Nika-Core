from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from _product_decision_test_support import ApprovedProductDecisionRepository
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
from nika_core.product_project_history_integrity import (
    ProductProjectHistoricalIntegrityService,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _project(tmp_path):
    store = SQLiteStore(tmp_path / "strict-portable-history.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Strict portable history",
        spec=ProductProjectSpec(
            goal="Preserve durable ProductProject history",
            desired_outcome="Reject ambiguous persisted history scalar types",
        ),
        idempotency_key="create:project-1",
    )
    return store, projects


def _research(projects: ProductProjectRepository) -> None:
    projects.record_research_handoff(
        "project-1",
        ResearchEvidencePackage(
            package_id="research-1",
            evidence=(
                EvidenceRef(
                    evidence_id="evidence-1",
                    provenance_ref="research://strict-history/1",
                    claim="Persisted source evidence",
                ),
            ),
        ),
        (
            ProductOption(
                option_id="option-1",
                title="Option one",
                summary="Use the measured direction",
                evidence_package_ids=("research-1",),
            ),
        ),
    )


def _decision(store: SQLiteStore, projects: ProductProjectRepository) -> None:
    _research(projects)
    current = projects.get("project-1")
    ProductDecisionRepository(store).record(
        "project-1",
        ProductDecision(
            decision_id="decision-1",
            option_id="option-1",
            state=ProductDecisionState.REJECTED,
            rationale="Reject this option after review",
            decided_by_ref="policy://product-owner",
        ),
        expected_row_version=current.row_version,
        idempotency_key="decision:decision-1:rejected",
    )


def test_history_rejects_fractional_durable_root_versions(tmp_path) -> None:
    store, _ = _project(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET row_version=0.5 WHERE project_id='project-1'"
        )
    with pytest.raises(ProductProjectError, match="row_version"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")

    store, _ = _project(tmp_path / "spec")
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET current_spec_version=1.5 "
            "WHERE project_id='project-1'"
        )
        conn.execute(
            "UPDATE product_project_specs SET spec_version=1.5 "
            "WHERE project_id='project-1'"
        )
    with pytest.raises(ProductProjectError, match="current_spec_version|spec_version"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


@pytest.mark.parametrize(
    ("keyword", "value"),
    (("expected_spec_version", True), ("expected_row_version", False)),
)
def test_history_rejects_boolean_expected_versions(tmp_path, keyword, value) -> None:
    store, _ = _project(tmp_path)
    with pytest.raises(ProductProjectError, match=keyword):
        ProductProjectHistoricalIntegrityService(store).validate(
            "project-1",
            **{keyword: value},
        )


def test_history_rejects_fractional_decision_version(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET decision_version=1.5 "
            "WHERE project_id='project-1' AND decision_id='decision-1'"
        )
    with pytest.raises(ProductProjectError, match="decision version"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_fractional_mutation_entity_version(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET entity_version=1.5 "
            "WHERE project_id='project-1' "
            "AND operation_kind='product_decision.record'"
        )
    with pytest.raises(ProductProjectError, match="entity_version"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_blob_idempotency_fingerprint(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET input_fingerprint=? "
            "WHERE project_id='project-1' "
            "AND operation_kind='product_decision.record'",
            (sqlite3.Binary(b"0" * 64),),
        )
    with pytest.raises(ProductProjectError, match="idempotency record"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_blob_creation_timestamp(tmp_path) -> None:
    store, _ = _project(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET created_at=? "
            "WHERE entity_type='product_project' AND entity_id='project-1' "
            "AND event_type='product_project.created'",
            (sqlite3.Binary(b"2026-10-05T00:00:00+00:00"),),
        )
    with pytest.raises(ProductProjectError, match="invalid timestamp"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_blob_research_payload_json(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects)
    with store.connection() as conn:
        raw = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='project-1' AND package_id='research-1'"
        ).fetchone()["payload_json"]
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id='project-1' AND package_id='research-1'",
            (sqlite3.Binary(raw.encode("utf-8")),),
        )
    with pytest.raises(ProductProjectError, match="invalid JSON"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_object_shaped_decision_evidence(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET evidence_package_ids_json=? "
            "WHERE project_id='project-1' AND decision_id='decision-1'",
            (json.dumps({"research-1": True}),),
        )
    with pytest.raises(ProductProjectError, match="invalid historical product decision"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_nontext_spec_revision_reason(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        replace(current.spec, hypothesis="strict audit text"),
        expected_row_version=current.row_version,
        change_reason="record strict audit text",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT event_id,payload_json FROM audit_events "
            "WHERE entity_type='product_project' AND entity_id='project-1' "
            "AND event_type='product_project.spec_versioned'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["change_reason"] = 7
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload), row["event_id"]),
        )
    with pytest.raises(ProductProjectError, match="spec revision audit"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_nontext_lifecycle_actor(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    ProductProjectLifecycleService(store).transition(
        "project-1",
        ProductProjectState.PAUSED,
        expected_row_version=current.row_version,
        idempotency_key="status:pause:strict-history",
        reason="Pause for strict portable history verification",
        changed_by_ref="policy://product-owner",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT event_id,payload_json FROM audit_events "
            "WHERE entity_type='product_project' AND entity_id='project-1' "
            "AND event_type='product_project.status_changed'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["changed_by_ref"] = 7
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload), row["event_id"]),
        )
    with pytest.raises(ProductProjectError, match="lifecycle audit"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def _different_sha256(value: str) -> str:
    candidate = "0" * 64
    return "1" * 64 if value == candidate else candidate


def test_history_rejects_tampered_create_idempotency_fingerprint(tmp_path) -> None:
    store, _ = _project(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT input_fingerprint FROM product_project_idempotency "
            "WHERE project_id='project-1'"
        ).fetchone()
        conn.execute(
            "UPDATE product_project_idempotency SET input_fingerprint=? "
            "WHERE project_id='project-1'",
            (_different_sha256(row["input_fingerprint"]),),
        )
    with pytest.raises(ProductProjectError, match="creation idempotency fingerprint"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_missing_decision_idempotency_receipt(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM product_project_mutation_idempotency "
            "WHERE project_id='project-1' AND operation_kind='product_decision.record'"
        )
    with pytest.raises(ProductProjectError, match="decision mutation lacks idempotency receipt"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_decision_idempotency_fingerprint_drift(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT input_fingerprint FROM product_project_mutation_idempotency "
            "WHERE project_id='project-1' AND operation_kind='product_decision.record'"
        ).fetchone()
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET input_fingerprint=? "
            "WHERE project_id='project-1' AND operation_kind='product_decision.record'",
            (_different_sha256(row["input_fingerprint"]),),
        )
    with pytest.raises(ProductProjectError, match="decision idempotency fingerprint"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_unknown_mutation_idempotency_kind(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET operation_kind=? "
            "WHERE project_id='project-1' AND operation_kind='product_decision.record'",
            ("product_project.unknown",),
        )
    with pytest.raises(ProductProjectError, match="unsupported .* operation kind"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_lifecycle_idempotency_fingerprint_drift(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    ProductProjectLifecycleService(store).transition(
        "project-1",
        ProductProjectState.PAUSED,
        expected_row_version=current.row_version,
        idempotency_key="status:pause:fingerprint-drift",
        reason="Pause for fingerprint verification",
        changed_by_ref="policy://product-owner",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT input_fingerprint FROM product_project_mutation_idempotency "
            "WHERE project_id='project-1' "
            "AND operation_kind='product_project.status_transition'"
        ).fetchone()
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET input_fingerprint=? "
            "WHERE project_id='project-1' "
            "AND operation_kind='product_project.status_transition'",
            (_different_sha256(row["input_fingerprint"]),),
        )
    with pytest.raises(ProductProjectError, match="lifecycle idempotency fingerprint"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_missing_modern_spec_idempotency_receipt(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        replace(current.spec, hypothesis="modern durable receipt"),
        expected_row_version=current.row_version,
        change_reason="record modern durable receipt",
        idempotency_key="spec:modern-receipt",
    )
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM product_project_spec_idempotency "
            "WHERE project_id='project-1' AND result_spec_version=2"
        )
    with pytest.raises(ProductProjectError, match="lack exact idempotency receipts"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_modern_spec_idempotency_fingerprint_drift(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        replace(current.spec, hypothesis="modern fingerprint authority"),
        expected_row_version=current.row_version,
        change_reason="record modern fingerprint authority",
        idempotency_key="spec:modern-fingerprint",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT input_fingerprint FROM product_project_spec_idempotency "
            "WHERE project_id='project-1' AND result_spec_version=2"
        ).fetchone()
        conn.execute(
            "UPDATE product_project_spec_idempotency SET input_fingerprint=? "
            "WHERE project_id='project-1' AND result_spec_version=2",
            (_different_sha256(row["input_fingerprint"]),),
        )
    with pytest.raises(ProductProjectError, match="spec idempotency fingerprint"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_create_idempotency_timestamp_drift(tmp_path) -> None:
    store, _ = _project(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_idempotency SET created_at=? "
            "WHERE project_id='project-1'",
            ("2030-01-01T00:00:00+00:00",),
        )
    with pytest.raises(ProductProjectError, match="creation idempotency timestamp drift"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_decision_idempotency_timestamp_drift(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _decision(store, projects)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET created_at=? "
            "WHERE project_id='project-1' AND operation_kind='product_decision.record'",
            ("2030-01-01T00:00:00+00:00",),
        )
    with pytest.raises(ProductProjectError, match="decision idempotency timestamp drift"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")


def test_history_rejects_lifecycle_idempotency_timestamp_drift(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    ProductProjectLifecycleService(store).transition(
        "project-1",
        ProductProjectState.PAUSED,
        expected_row_version=current.row_version,
        idempotency_key="status:pause:timestamp-drift",
        reason="Pause for timestamp verification",
        changed_by_ref="policy://product-owner",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET created_at=? "
            "WHERE project_id='project-1' "
            "AND operation_kind='product_project.status_transition'",
            ("2030-01-01T00:00:00+00:00",),
        )
    with pytest.raises(ProductProjectError, match="lifecycle idempotency timestamp drift"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")

def test_history_accepts_trusted_approved_decision_writer_fingerprint(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects)
    current = projects.get("project-1")
    stored = ApprovedProductDecisionRepository(store).record(
        "project-1",
        ProductDecision(
            decision_id="decision-approved",
            option_id="option-1",
            state=ProductDecisionState.APPROVED,
            rationale="Approve the evidence-backed option",
            decided_by_ref="user://owner",
        ),
        expected_row_version=current.row_version,
        idempotency_key="decision:approved:history-integrity",
    )

    assert stored.decision.decided_by_ref.startswith("approval://")
    report = ProductProjectHistoricalIntegrityService(store).validate("project-1")
    assert report.mutation_idempotency_count == 1


def test_history_rejects_approved_decision_actor_drift(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects)
    current = projects.get("project-1")
    ApprovedProductDecisionRepository(store).record(
        "project-1",
        ProductDecision(
            decision_id="decision-approved",
            option_id="option-1",
            state=ProductDecisionState.APPROVED,
            rationale="Approve the evidence-backed option",
            decided_by_ref="user://owner",
        ),
        expected_row_version=current.row_version,
        idempotency_key="decision:approved:actor-drift",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET decided_by_ref=? "
            "WHERE project_id='project-1' AND decision_id='decision-approved'",
            ("approval://" + "0" * 64,),
        )

    with pytest.raises(ProductProjectError, match="decision audit actor drift"):
        ProductProjectHistoricalIntegrityService(store).validate("project-1")

