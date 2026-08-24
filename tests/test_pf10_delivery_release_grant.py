from __future__ import annotations

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
from nika_core.product_compliance import ProductComplianceGate
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)


class _AllowScopeReview:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (
            project_id == "product-release-boundary"
            and evidence_ref == "review:scope:product-release-boundary"
            and purpose == "compliance-scope"
        )


def _base_pf10_decision():
    decision = ProductComplianceGate(
        review_authority=_AllowScopeReview(),
    ).evaluate(
        project_id="product-release-boundary",
        scope_review_ref="review:scope:product-release-boundary",
    )
    assert decision.allowed is True
    return decision


def _ready_factory(tmp_path, business_authority) -> BusinessFactory:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-release-boundary",
            goal="Build the exact release-boundary artifact",
            research_package=ResearchEvidencePackage(
                package_id="research-release-boundary",
                evidence=(
                    EvidenceRef(
                        "evidence-release-boundary",
                        "research:public:release-boundary",
                        "Demand evidence",
                    ),
                ),
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-release-boundary",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
        approval_authority=business_authority,
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-release-boundary",
        title="Release-boundary artifact",
        evidence_ids=("evidence-release-boundary",),
    )
    factory.create_lead(
        lead_id="lead-release-boundary",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:release-boundary",
    )
    factory.qualify_lead(qualification_ref="qualification:release-boundary")
    factory.draft_proposal(
        proposal_id="proposal-release-boundary",
        scope_summary="Build the exact release-boundary artifact.",
    )
    business_authority.allow_once("approval:proposal:release-boundary")
    factory.approve_proposal(approval_ref="approval:proposal:release-boundary")
    business_authority.allow_once("approval:work-order:release-boundary")
    factory.create_work_order(
        work_order_id="work-order-release-boundary",
        scope="Build the exact release-boundary artifact.",
        authorization_ref="approval:work-order:release-boundary",
    )

    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="product-release-boundary",
        project_name="Release Boundary Product",
        spec=ProductProjectSpec(
            goal="Build the exact release-boundary artifact",
            desired_outcome="A QA-reviewed exact artifact",
            compliance={"business_work_order_ref": "work-order-release-boundary"},
        ),
        idempotency_key="handoff:release-boundary",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:release-boundary:passed")
    return factory


def test_gate_issued_base_pf10_decision_cannot_bypass_exact_release_grant(
    tmp_path,
    business_authority,
) -> None:
    factory = _ready_factory(tmp_path, business_authority)
    base_decision = _base_pf10_decision()
    business_authority.allow_once("approval:delivery:release-boundary")

    with pytest.raises(BusinessFactoryError, match="exact PF10 release compliance grant"):
        factory.record_delivery(
            delivery_id="delivery-release-boundary",
            artifact_ref="artifact:release-boundary:1",
            authorization_ref="approval:delivery:release-boundary",
            compliance=base_decision,  # type: ignore[arg-type]
        )
