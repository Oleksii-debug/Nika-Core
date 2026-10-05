from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from nika_core.security import ApprovalAuthority

_NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


class _TextCarrier(str):
    pass


class _DecisionCarrier:
    def __init__(self, decision: ProductDecision) -> None:
        self.decision_id = decision.decision_id
        self.option_id = decision.option_id
        self.state = decision.state
        self.rationale = decision.rationale
        self.decided_by_ref = decision.decided_by_ref


def _setup(tmp_path: Path):
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
                summary="Owner approval must come from trusted host evidence.",
                evidence_package_ids=("research-1",),
            ),
        ),
    )
    decision = ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Evidence-backed owner choice.",
        decided_by_ref="user://owner",
    )
    return store, projects, decision


@pytest.mark.parametrize(
    "case",
    (
        "project-id-carrier",
        "idempotency-carrier",
        "decision-carrier",
        "decision-id-carrier",
        "option-id-carrier",
        "state-carrier",
        "rationale-carrier",
        "attribution-carrier",
    ),
)
def test_owner_decision_boundary_rejects_nonexact_input_carriers(
    tmp_path: Path,
    case: str,
) -> None:
    store, projects, decision = _setup(tmp_path)
    decisions = ProductDecisionRepository(store)
    project_id: str = "p1"
    idempotency_key: str = "decision:carrier"
    candidate: object = decision

    if case == "project-id-carrier":
        project_id = _TextCarrier(project_id)
    elif case == "idempotency-carrier":
        idempotency_key = _TextCarrier(idempotency_key)
    elif case == "decision-carrier":
        candidate = _DecisionCarrier(decision)
    else:
        field, value = {
            "decision-id-carrier": ("decision_id", _TextCarrier(decision.decision_id)),
            "option-id-carrier": ("option_id", _TextCarrier(decision.option_id)),
            "state-carrier": ("state", decision.state.value),
            "rationale-carrier": ("rationale", _TextCarrier(decision.rationale)),
            "attribution-carrier": (
                "decided_by_ref",
                _TextCarrier(decision.decided_by_ref),
            ),
        }[case]
        object.__setattr__(decision, field, value)

    with pytest.raises(ProductProjectError, match="exact"):
        decisions.approval_intent(
            project_id,
            candidate,  # type: ignore[arg-type]
            expected_row_version=0,
            idempotency_key=idempotency_key,
        )

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions WHERE project_id='p1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' AND operation_kind='product_decision.record'"
        ).fetchone()[0] == 0


def test_caller_text_cannot_mint_product_owner_approval(tmp_path: Path) -> None:
    store, projects, decision = _setup(tmp_path)
    decisions = ProductDecisionRepository(store)

    with pytest.raises(PermissionError, match="trusted product-owner approval"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:forged-owner-approval",
        )

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM product_decisions WHERE project_id='p1'"
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' AND operation_kind='product_decision.record'"
        ).fetchone()["count"] == 0


def test_exact_trusted_approval_commits_and_replay_needs_no_second_approval(
    tmp_path: Path,
) -> None:
    store, projects, decision = _setup(tmp_path)
    authority = ApprovalAuthority()
    decisions = ProductDecisionRepository(
        store,
        approval_verifier=authority.verifier(),
    )
    intent = decisions.approval_intent(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:approved",
    )
    request = authority.request(intent, now=_NOW)
    approval = authority.approve(
        request.request_id,
        now=_NOW + timedelta(seconds=1),
    )

    stored = decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:approved",
        approval=approval,
        now=_NOW + timedelta(seconds=2),
    )
    replay = decisions.record(
        "p1",
        replace(decision, decided_by_ref="user://spoofed-owner"),
        expected_row_version=0,
        idempotency_key="decision:approved",
    )

    assert replay == stored
    assert stored.decision.decided_by_ref.startswith("approval://")
    assert stored.decision.decided_by_ref != decision.decided_by_ref
    assert projects.get("p1").row_version == 1
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE event_type='product_project.decision_recorded' "
            "AND entity_type='product_project' AND entity_id='p1'"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["approval_authority"]["approval_id"] == approval.approval_id
    assert payload["approval_authority"]["issuer_id"] == approval.issuer_id
    assert payload["decided_by_ref"] == stored.decision.decided_by_ref
    assert "user://owner" not in payload["decided_by_ref"]


def test_approval_is_bound_to_exact_evidence_bytes(tmp_path: Path) -> None:
    store, projects, decision = _setup(tmp_path)
    authority = ApprovalAuthority()
    decisions = ProductDecisionRepository(
        store,
        approval_verifier=authority.verifier(),
    )
    intent = decisions.approval_intent(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:evidence-bound",
    )
    request = authority.request(intent, now=_NOW)
    approval = authority.approve(
        request.request_id,
        now=_NOW + timedelta(seconds=1),
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id='p1' AND package_id='research-1'",
            (row["payload_json"] + " ",),
        )

    with pytest.raises(PermissionError, match="exact action|approval"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:evidence-bound",
            approval=approval,
            now=_NOW + timedelta(seconds=2),
        )

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM product_decisions WHERE project_id='p1'"
        ).fetchone()["count"] == 0
