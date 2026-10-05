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
from nika_core.security import ApprovalAuthority

_TEST_SECRET = b"nika-pf1-owner-authority-regression-seed-0001"


def _environment(tmp_path):
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
                    claim="Synthetic non-secret authority evidence",
                ),
            ),
        ),
        (
            ProductOption(
                option_id="option-1",
                title="Candidate-selected option",
                summary="Final state requires trusted owner authority.",
                evidence_package_ids=("research-1",),
            ),
        ),
    )
    return store, projects


def _decision(
    *,
    state: ProductDecisionState = ProductDecisionState.APPROVED,
    rationale: str = "Owner accepts the evidence-backed option.",
) -> ProductDecision:
    return ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=state,
        rationale=rationale,
        decided_by_ref="user://owner",
    )


def _trusted_repository(store: SQLiteStore):
    authority = ApprovalAuthority(
        issuer_id="test-pf1-product-owner",
        secret=_TEST_SECRET,
    )
    repository = ProductDecisionRepository(
        store,
        approval_verifier=authority.verifier(),
    )
    return authority, repository


def _issue(
    authority: ApprovalAuthority,
    repository: ProductDecisionRepository,
    decision: ProductDecision,
    *,
    task_id: str = "task-product-owner-review",
    expected_row_version: int = 0,
):
    intent = repository.approval_intent(
        "p1",
        decision,
        expected_row_version=expected_row_version,
        task_id=task_id,
    )
    request = authority.request(intent)
    return authority.approve(request.request_id)


def test_caller_cannot_self_assert_product_owner_approval(tmp_path) -> None:
    store, projects = _environment(tmp_path)
    decisions = ProductDecisionRepository(store)

    with pytest.raises(PermissionError, match="product-owner approval authority"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:forged-owner-approval",
        )

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM product_project_mutation_idempotency "
                "WHERE project_id='p1' AND operation_kind='product_decision.record'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE entity_type='product_project' AND entity_id='p1' "
                "AND event_type='product_project.decision_recorded'"
            ).fetchone()[0]
            == 0
        )


def test_trusted_approval_replaces_caller_attribution_and_survives_restart(
    tmp_path,
) -> None:
    store, projects = _environment(tmp_path)
    authority, decisions = _trusted_repository(store)
    decision = _decision()
    approval = _issue(authority, decisions, decision)

    stored = decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:trusted-owner",
        approval=approval,
        approval_task_id="task-product-owner-review",
    )

    assert projects.get("p1").row_version == 1
    assert stored.decision.decided_by_ref.startswith("approval://")
    assert stored.decision.decided_by_ref != "user://owner"
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE event_type='product_project.decision_recorded' "
            "AND entity_type='product_project' AND entity_id='p1'"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["approval"]["approval_ref"] == stored.decision.decided_by_ref
    assert payload["approval"]["approval_id"] == approval.approval_id
    assert payload["approval"]["issuer_id"] == approval.issuer_id
    assert payload["approval"]["task_id"] == "task-product-owner-review"
    assert "signature" not in payload["approval"]

    restarted = ProductDecisionRepository(SQLiteStore(store.path))
    replay = restarted.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:trusted-owner",
    )
    assert replay == stored


def test_approval_is_bound_to_exact_task_context(tmp_path) -> None:
    store, projects = _environment(tmp_path)
    authority, decisions = _trusted_repository(store)
    decision = _decision()
    approval = _issue(authority, decisions, decision, task_id="task-a")

    with pytest.raises(PermissionError, match="exact action|current decision authority"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:wrong-task",
            approval=approval,
            approval_task_id="task-b",
        )

    assert projects.get("p1").row_version == 0


def test_approval_is_invalidated_if_evidence_changes_before_commit(tmp_path) -> None:
    store, projects = _environment(tmp_path)
    authority, decisions = _trusted_repository(store)
    decision = _decision()
    approval = _issue(authority, decisions, decision)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["options"][0]["summary"] = "Evidence presentation changed after approval."
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id='p1' AND package_id='research-1'",
            (json.dumps(payload),),
        )

    with pytest.raises(PermissionError, match="current decision authority"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:stale-evidence",
            approval=approval,
            approval_task_id="task-product-owner-review",
        )

    assert projects.get("p1").row_version == 0


def test_proposed_decision_remains_agent_recordable_without_owner_approval(
    tmp_path,
) -> None:
    store, projects = _environment(tmp_path)
    decisions = ProductDecisionRepository(store)
    proposed = _decision(
        state=ProductDecisionState.PROPOSED,
        rationale="Agent proposal awaiting owner review.",
    )

    stored = decisions.record(
        "p1",
        proposed,
        expected_row_version=0,
        idempotency_key="decision:proposed",
    )

    assert stored.decision.state is ProductDecisionState.PROPOSED
    assert stored.decision.decided_by_ref == "user://owner"
    assert projects.get("p1").row_version == 1


def test_rejected_final_decision_also_requires_trusted_owner_approval(tmp_path) -> None:
    store, projects = _environment(tmp_path)
    decisions = ProductDecisionRepository(store)

    with pytest.raises(PermissionError, match="product-owner approval authority"):
        decisions.record(
            "p1",
            _decision(
                state=ProductDecisionState.REJECTED,
                rationale="Owner rejects this option.",
            ),
            expected_row_version=0,
            idempotency_key="decision:forged-rejection",
        )

    assert projects.get("p1").row_version == 0
