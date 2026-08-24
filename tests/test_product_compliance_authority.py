from __future__ import annotations

import re
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
    DependencyAdoption,
    LicenseDisposition,
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

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class _ReviewAuthority:
    def __init__(self, grants: tuple[tuple[str, str, str], ...]) -> None:
        self._grants = grants

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        for expected_project, expected_ref, purpose_prefix in self._grants:
            if project_id != expected_project or evidence_ref != expected_ref:
                continue
            prefix = purpose_prefix + ":"
            if purpose.startswith(prefix):
                return _FINGERPRINT_RE.fullmatch(purpose.removeprefix(prefix)) is not None
        return False


class _BrokenReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        raise RuntimeError(f"authority unavailable for {project_id}:{purpose}:{evidence_ref}")


def _reviewed_empty_gate(project_id: str, closure_ref: str, scope_ref: str) -> ProductComplianceGate:
    return ProductComplianceGate(
        review_authority=_ReviewAuthority(
            (
                (project_id, closure_ref, "dependency-closure"),
                (project_id, scope_ref, "compliance-scope"),
            )
        )
    )


def _reviewed_empty_decision(
    project_id: str,
    closure_ref: str,
    scope_ref: str,
) -> tuple[ProductComplianceGate, ProductComplianceDecision]:
    gate = _reviewed_empty_gate(project_id, closure_ref, scope_ref)
    decision = gate.evaluate(
        project_id=project_id,
        dependency_closure_ref=closure_ref,
        scope_review_ref=scope_ref,
    )
    return gate, decision


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
    project_id = "project-1"
    closure_ref = "review:dependency-closure:project-1"
    scope_ref = "review:compliance-scope:project-1"
    gate, decision = _reviewed_empty_decision(project_id, closure_ref, scope_ref)

    assert decision.allowed is True
    assert decision.findings == ()
    assert closure_ref in decision.evidence_refs
    assert scope_ref in decision.evidence_refs
    gate.require_release_allowed(
        decision,
        project_id=project_id,
        dependencies=(),
        obligation_evidence=(),
        competitor_evidence=(),
        dependency_closure_ref=closure_ref,
        scope_review_ref=scope_ref,
    )


def test_positive_decision_tamper_invalidates_authority() -> None:
    project_id = "project-1"
    closure_ref = "review:dependency-closure:project-1"
    scope_ref = "review:compliance-scope:project-1"
    gate, decision = _reviewed_empty_decision(project_id, closure_ref, scope_ref)
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

    gate.require_release_allowed(
        decision,
        project_id=project_id,
        dependencies=(),
        obligation_evidence=(),
        competitor_evidence=(),
        dependency_closure_ref=closure_ref,
        scope_review_ref=scope_ref,
    )


def test_missing_compliance_scope_or_dependency_closure_blocks_empty_inventory_false_green() -> None:
    unreviewed = ProductComplianceGate().evaluate(project_id="project-1")

    assert unreviewed.allowed is False
    assert "dependency-closure:unverified" in unreviewed.findings
    assert "compliance-scope:unreviewed" in unreviewed.findings
    with pytest.raises(ProductComplianceError, match="dependency-closure:unverified"):
        ProductComplianceGate().require_release_allowed(unreviewed)


def test_opaque_scope_and_closure_review_text_is_not_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependency_closure_ref="caller:claims-dependency-closure-reviewed",
        scope_review_ref="caller:claims-review-happened",
    )

    assert decision.allowed is False
    assert "dependency-closure:untrusted-review-authority" in decision.findings
    assert "compliance-scope:untrusted-review-authority" in decision.findings


def test_opaque_dependency_review_text_is_not_license_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependencies=(
            DependencyAdoption(
                project_id="project-1",
                component_id="component-aud05",
                package_name="example-package",
                version="1.0.0",
                source_ref="registry:pypi:example-package:1.0.0",
                provenance_ref="sha256:" + ("a" * 64),
                license_expression="MIT",
                license_disposition=LicenseDisposition.APPROVED,
                review_ref="caller:claims-license-review-happened",
            ),
        ),
    )

    assert decision.allowed is False
    assert "license:untrusted-review-authority:component-aud05" in decision.findings


def test_explicit_trusted_review_can_authorize_empty_compliance_inventory() -> None:
    project_id = "project-no-third-party-components"
    closure_ref = "review:dependency-closure:empty-inventory:1"
    scope_ref = "review:compliance-scope:empty-inventory:1"
    gate, reviewed = _reviewed_empty_decision(project_id, closure_ref, scope_ref)

    assert reviewed.allowed is True
    gate.require_release_allowed(
        reviewed,
        project_id=project_id,
        dependencies=(),
        obligation_evidence=(),
        competitor_evidence=(),
        dependency_closure_ref=closure_ref,
        scope_review_ref=scope_ref,
    )


def test_review_authority_failure_is_fail_closed() -> None:
    decision = ProductComplianceGate(review_authority=_BrokenReviewAuthority()).evaluate(
        project_id="project-1",
        dependency_closure_ref="review:dependency-closure:project-1",
        scope_review_ref="review:compliance-scope:project-1",
    )

    assert decision.allowed is False
    assert "dependency-closure:untrusted-review-authority" in decision.findings
    assert "compliance-scope:untrusted-review-authority" in decision.findings


def test_business_delivery_rejects_caller_fabricated_positive_decision(
    tmp_path,
    business_authority,
) -> None:
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
        approval_authority=business_authority,
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
    business_authority.allow_once("approval:proposal:authority")
    factory.approve_proposal(approval_ref="approval:proposal:authority")
    business_authority.allow_once("approval:work-order:authority")
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
