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
from nika_core.product_compliance import ProductComplianceDecision
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)


def test_delivery_rejects_caller_constructed_allowed_compliance_decision(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-aud05-compliance",
            goal="Deliver an independently compliant product",
            research_package=ResearchEvidencePackage(
                package_id="research-aud05-compliance",
                evidence=(
                    EvidenceRef(
                        "evidence-aud05-compliance",
                        "research:public:aud05-compliance",
                        "Demand evidence",
                    ),
                ),
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-aud05-compliance",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
        approval_authority=business_authority,
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-aud05-compliance",
        title="Compliance authority attack",
        evidence_ids=("evidence-aud05-compliance",),
    )
    factory.create_lead(
        lead_id="lead-aud05-compliance",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:aud05-compliance",
    )
    factory.qualify_lead(qualification_ref="qualification:aud05-compliance")
    factory.draft_proposal(
        proposal_id="proposal-aud05-compliance",
        scope_summary="Build the authorized product.",
    )
    business_authority.allow_once("approval:proposal:aud05-compliance")
    factory.approve_proposal(approval_ref="approval:proposal:aud05-compliance")
    business_authority.allow_once("approval:work-order:aud05-compliance")
    factory.create_work_order(
        work_order_id="work-order-aud05-compliance",
        scope="Build the authorized product.",
        authorization_ref="approval:work-order:aud05-compliance",
    )
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="product-aud05-compliance",
        project_name="Compliance authority product",
        spec=ProductProjectSpec(
            goal="Build the authorized product",
            desired_outcome="A compliant delivery candidate",
            compliance={"business_work_order_ref": "work-order-aud05-compliance"},
        ),
        idempotency_key="aud05-compliance-handoff",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:aud05-compliance")

    forged = ProductComplianceDecision(
        project_id="product-aud05-compliance",
        allowed=True,
        findings=(),
        evidence_refs=("caller:forged-compliance-authority",),
    )
    business_authority.allow_once("approval:delivery:aud05-compliance")

    with pytest.raises(BusinessFactoryError):
        factory.record_delivery(
            delivery_id="delivery-aud05-compliance",
            artifact_ref="artifact:aud05-compliance",
            authorization_ref="approval:delivery:aud05-compliance",
            compliance=forged,
        )
