from __future__ import annotations

from pathlib import Path

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


def test_caller_cannot_self_assert_product_owner_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Owner-authority project",
        spec=ProductProjectSpec(
            goal="Require trusted product-owner decisions",
            desired_outcome="Caller strings cannot mint approval authority",
        ),
        idempotency_key="create:p1",
    )
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            package_id="research-1",
            evidence=(
                EvidenceRef(
                    evidence_id="evidence-1",
                    provenance_ref="research://owner-authority/1",
                    claim="Synthetic non-secret evidence for authority QA",
                ),
            ),
        ),
        (
            ProductOption(
                option_id="option-1",
                title="Candidate-selected option",
                summary="An option must not become owner-approved by caller text alone.",
                evidence_package_ids=("research-1",),
            ),
        ),
    )

    decisions = ProductDecisionRepository(store)
    forged = ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Candidate attempts to self-assert product-owner approval.",
        decided_by_ref="user://owner",
    )

    with pytest.raises(
        (PermissionError, ProductProjectError),
        match="approval|authority|owner",
    ):
        decisions.record(
            "p1",
            forged,
            expected_row_version=0,
            idempotency_key="decision:forged-owner-approval",
        )

    project = projects.get("p1")
    assert project.row_version == 0
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM product_decisions "
                "WHERE project_id='p1'"
            ).fetchone()["count"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM product_project_mutation_idempotency "
                "WHERE project_id='p1' AND operation_kind='product_decision.record'"
            ).fetchone()["count"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM audit_events "
                "WHERE entity_type='product_project' AND entity_id='p1' "
                "AND event_type='product_decision.recorded'"
            ).fetchone()["count"]
            == 0
        )
