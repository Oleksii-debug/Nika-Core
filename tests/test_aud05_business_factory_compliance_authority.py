from __future__ import annotations

from pathlib import Path

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


def test_delivery_rejects_caller_minted_allowed_compliance_decision(tmp_path: Path) -> None:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-aud05",
            goal="Controlled QA product",
            research_package=ResearchEvidencePackage(
                package_id="research-aud05",
                evidence=(EvidenceRef("evidence-1", "public:source:1", "evidence"),),
                research_artifact_ref="research:artifact:1",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-aud05",
            allowed_channel_ids=("sandbox",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-aud05",
        title="Controlled opportunity",
        evidence_ids=("evidence-1",),
    )
    factory.create_lead(
        lead_id="lead-aud05",
        channel_id="sandbox",
        counterparty_ref="counterparty:test",
    )
    factory.qualify_lead(qualification_ref="candidate:self-qualification")
    factory.draft_proposal(
        proposal_id="proposal-aud05",
        scope_summary="Controlled scope",
    )
    factory.approve_proposal(approval_ref="candidate:self-approval")
    factory.create_work_order(
        work_order_id="work-order-aud05",
        scope="Controlled scope",
        authorization_ref="candidate:self-work-order-authorization",
    )

    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="project-aud05",
        project_name="AUD05 controlled project",
        spec=ProductProjectSpec(
            goal="Build controlled product",
            desired_outcome="Verified artifact",
            compliance={"business_work_order_ref": "work-order-aud05"},
        ),
        idempotency_key="request-aud05",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="candidate:self-qa-pass")

    # This object did not come from ProductComplianceGate or any host authority.
    forged = ProductComplianceDecision(
        project_id="project-aud05",
        allowed=True,
        findings=(),
        evidence_refs=(),
    )

    with pytest.raises(BusinessFactoryError):
        factory.record_delivery(
            delivery_id="delivery-aud05",
            artifact_ref="artifact:test",
            authorization_ref="candidate:self-delivery-authorization",
            compliance=forged,
        )
