from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactoryError,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
    QAState,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_compliance import (
    ProductComplianceDecision,
    ProductComplianceError,
    ProductComplianceGate,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)


def test_caller_constructed_positive_decision_has_no_release_authority() -> None:
    forged = ProductComplianceDecision(
        project_id="project-1",
        allowed=True,
        findings=(),
        evidence_refs=("caller:asserted-compliance",),
    )

    assert forged.allowed is False
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(forged)


def test_gate_issued_positive_decision_has_process_local_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        scope_review_ref="review:compliance-scope:project-1",
    )

    assert decision.allowed is True
    assert decision.findings == ()
    assert "review:compliance-scope:project-1" in decision.evidence_refs
    ProductComplianceGate().require_release_allowed(decision)


def test_positive_decision_tamper_invalidates_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        scope_review_ref="review:compliance-scope:project-1",
    )
    assert decision.allowed is True

    project_substitution = replace(decision, project_id="project-2")
    evidence_substitution = replace(
        decision,
        evidence_refs=("review:attacker-substitution",),
    )

    assert project_substitution.allowed is False
    assert evidence_substitution.allowed is False
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(project_substitution)
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(evidence_substitution)


def test_missing_compliance_scope_review_blocks_empty_inventory_false_green() -> None:
    unreviewed = ProductComplianceGate().evaluate(project_id="project-1")

    assert unreviewed.allowed is False
    assert "compliance-scope:unreviewed" in unreviewed.findings
    with pytest.raises(ProductComplianceError, match="compliance-scope:unreviewed"):
        ProductComplianceGate().require_release_allowed(unreviewed)


def test_explicit_review_can_authorize_legitimately_empty_compliance_inventory() -> None:
    reviewed = ProductComplianceGate().evaluate(
        project_id="project-no-third-party-components",
        scope_review_ref="review:compliance-scope:empty-inventory:1",
    )

    assert reviewed.allowed is True
    ProductComplianceGate().require_release_allowed(reviewed)


def test_business_delivery_rejects_caller_fabricated_positive_decision(tmp_path) -> None:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-authority",
            goal="Build an authorized test product",
            research_package=ResearchEvidencePackage(
                package_id="research-authority",
                evidence=(
                    EvidenceRef(
                        "evidence-authority",
                        "research:public:authority",
                        "Demand evidence",
                    ),
                ),
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-authority",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-authority",
        title="Authorized test product",
        evidence_ids=("evidence-authority",),
    )
    factory.create_lead(
        lead_id="lead-authority",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:authority",
    )
    factory.qualify_lead(qualification_ref="qualification:authority")
    factory.draft_proposal(
        proposal_id="proposal-authority",
        scope_summary="Build the authorized test product.",
    )
    factory.approve_proposal(approval_ref="approval:proposal:authority")
    factory.create_work_order(
        work_order_id="work-order-authority",
        scope="Build the authorized test product.",
        authorization_ref="approval:work-order:authority",
    )

    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="product-authority",
        project_name="Authority Test Product",
        spec=ProductProjectSpec(
            goal="Build the authorized test product",
            desired_outcome="A QA-reviewed artifact",
            compliance={"business_work_order_ref": "work-order-authority"},
        ),
        idempotency_key="handoff-request:authority",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:authority:passed")

    forged = ProductComplianceDecision(
        project_id="product-authority",
        allowed=True,
        findings=(),
        evidence_refs=("caller:asserted-compliance",),
    )
    assert forged.allowed is False

    with pytest.raises(BusinessFactoryError, match="allowed PF10 compliance decision"):
        factory.record_delivery(
            delivery_id="delivery-authority",
            artifact_ref="artifact:authority:1",
            authorization_ref="approval:delivery:authority",
            compliance=forged,
        )
