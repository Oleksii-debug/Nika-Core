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
    DependencyAdoption,
    LicenseDisposition,
    PackagedDependencyEvidence,
    ProductComplianceDecision,
    ProductComplianceError,
    ProductComplianceGate,
    ProductComplianceSnapshot,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)

SOURCE_SHA = "1" * 64
NOTICE_SHA = "2" * 64


class _ReviewAuthority:
    def __init__(self, grants: tuple[tuple[str, str, str], ...]) -> None:
        self._grants = frozenset(grants)

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (project_id, evidence_ref, purpose) in self._grants


class _BrokenReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        raise RuntimeError(f"authority unavailable for {project_id}:{purpose}:{evidence_ref}")


class _PackagingAuthority:
    def __init__(
        self,
        project_id: str,
        inventory: tuple[PackagedDependencyEvidence, ...] = (),
    ) -> None:
        self._project_id = project_id
        self._inventory = inventory

    def inventory(self, *, project_id: str) -> tuple[PackagedDependencyEvidence, ...]:
        if project_id != self._project_id:
            raise PermissionError("wrong project")
        return self._inventory

    def verify_notice(
        self,
        *,
        project_id: str,
        package: PackagedDependencyEvidence,
    ) -> bool:
        return project_id == self._project_id and package in self._inventory


def _empty_snapshot(project_id: str, review_ref: str) -> ProductComplianceSnapshot:
    return ProductComplianceSnapshot(
        project_id=project_id,
        dependencies=(),
        obligation_evidence=(),
        competitor_evidence=(),
        packaged_dependencies=(),
        scope_review_ref=review_ref,
    )


def _scope_gate(
    project_id: str,
    review_ref: str,
) -> tuple[ProductComplianceGate, ProductComplianceSnapshot]:
    snapshot = _empty_snapshot(project_id, review_ref)
    gate = ProductComplianceGate(
        review_authority=_ReviewAuthority(
            ((project_id, review_ref, snapshot.scope_review_purpose),)
        ),
        packaging_authority=_PackagingAuthority(project_id),
    )
    return gate, snapshot


def test_caller_constructed_positive_decision_has_no_release_authority() -> None:
    forged = ProductComplianceDecision(
        project_id="project-1",
        snapshot_fingerprint="0" * 64,
        allowed=True,
        findings=(),
        evidence_refs=("caller:asserted-compliance",),
    )

    assert forged.allowed is False
    with pytest.raises(ProductComplianceError, match="untrusted-origin"):
        ProductComplianceGate().require_release_allowed(
            forged,
            current_snapshot=ProductComplianceSnapshot(
                project_id="project-1",
                dependencies=(),
                obligation_evidence=(),
                competitor_evidence=(),
                packaged_dependencies=(),
            ),
        )


def test_gate_issued_positive_decision_is_bound_to_exact_snapshot() -> None:
    review_ref = "review:compliance-scope:project-1"
    gate, snapshot = _scope_gate("project-1", review_ref)
    decision = gate.evaluate(
        project_id="project-1",
        scope_review_ref=review_ref,
    )

    assert decision.allowed is True
    assert decision.findings == ()
    assert review_ref in decision.evidence_refs
    gate.require_release_allowed(decision, current_snapshot=snapshot)


def test_positive_decision_tamper_invalidates_authority() -> None:
    review_ref = "review:compliance-scope:project-1"
    gate, snapshot = _scope_gate("project-1", review_ref)
    decision = gate.evaluate(project_id="project-1", scope_review_ref=review_ref)
    assert decision.allowed is True

    project_substitution = replace(decision, project_id="project-2")
    snapshot_substitution = replace(decision, snapshot_fingerprint="f" * 64)
    evidence_substitution = replace(
        decision,
        evidence_refs=("review:attacker-substitution",),
    )

    assert project_substitution.allowed is False
    assert snapshot_substitution.allowed is False
    assert evidence_substitution.allowed is False
    for forged in (project_substitution, snapshot_substitution, evidence_substitution):
        with pytest.raises(ProductComplianceError):
            gate.require_release_allowed(forged, current_snapshot=snapshot)


def test_missing_compliance_scope_review_blocks_empty_inventory_false_green() -> None:
    unreviewed = ProductComplianceGate(
        packaging_authority=_PackagingAuthority("project-1")
    ).evaluate(project_id="project-1")

    assert unreviewed.allowed is False
    assert "compliance-scope:unreviewed" in unreviewed.findings


def test_opaque_scope_review_text_is_not_authority() -> None:
    decision = ProductComplianceGate(
        packaging_authority=_PackagingAuthority("project-1")
    ).evaluate(
        project_id="project-1",
        scope_review_ref="caller:claims-review-happened",
    )

    assert decision.allowed is False
    assert "compliance-scope:untrusted-review-authority" in decision.findings


def test_opaque_dependency_review_text_is_not_license_authority() -> None:
    dependency = DependencyAdoption(
        project_id="project-1",
        component_id="component-aud05",
        package_name="example-package",
        version="1.0.0",
        source_ref="registry:pypi:example-package:1.0.0",
        source_sha256=SOURCE_SHA,
        provenance_ref="provenance:example-package",
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        notice_required=True,
        notice_refs=("artifact:THIRD_PARTY_NOTICES.txt#example-package@1.0.0",),
        review_ref="caller:claims-license-review-happened",
    )
    package = PackagedDependencyEvidence(
        package_name="example-package",
        version="1.0.0",
        notice_ref=dependency.notice_refs[0],
        notice_sha256=NOTICE_SHA,
    )
    decision = ProductComplianceGate(
        packaging_authority=_PackagingAuthority("project-1", (package,))
    ).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
    )

    assert decision.allowed is False
    assert "license:untrusted-review-authority:component-aud05" in decision.findings


def test_explicit_trusted_review_can_authorize_verified_empty_inventory() -> None:
    project_id = "project-no-third-party-components"
    review_ref = "review:compliance-scope:empty-inventory:1"
    gate, snapshot = _scope_gate(project_id, review_ref)
    reviewed = gate.evaluate(project_id=project_id, scope_review_ref=review_ref)

    assert reviewed.allowed is True
    gate.require_release_allowed(reviewed, current_snapshot=snapshot)


def test_review_authority_failure_is_fail_closed() -> None:
    project_id = "project-1"
    review_ref = "review:compliance-scope:project-1"
    decision = ProductComplianceGate(
        review_authority=_BrokenReviewAuthority(),
        packaging_authority=_PackagingAuthority(project_id),
    ).evaluate(
        project_id=project_id,
        scope_review_ref=review_ref,
    )

    assert decision.allowed is False
    assert "compliance-scope:untrusted-review-authority" in decision.findings


def test_packaging_authority_is_project_bound() -> None:
    gate = ProductComplianceGate(packaging_authority=_PackagingAuthority("other-project"))
    decision = gate.evaluate(project_id="project-1")
    assert decision.allowed is False
    assert "packaging:untrusted-inventory-authority" in decision.findings


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
        snapshot_fingerprint="0" * 64,
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
