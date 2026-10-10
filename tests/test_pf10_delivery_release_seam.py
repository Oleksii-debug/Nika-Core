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
from nika_core.product_compliance import (
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceGate,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)


class _ReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (
            project_id == "product-project-1"
            and evidence_ref == "review:license:httpx"
            and purpose.startswith("license-disposition:component-httpx")
        ) or (
            project_id == "product-project-1"
            and evidence_ref == "review:scope"
            and purpose.startswith("compliance-scope:")
        )


def _factory(business_authority) -> BusinessFactory:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-1",
            goal="Build a controlled test product",
            research_package=ResearchEvidencePackage(
                package_id="research-1",
                evidence=(EvidenceRef("evidence-1", "public:test", "fixture"),),
                research_artifact_ref="research:result-set:1",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-1",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
        approval_authority=business_authority,
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Fixture opportunity",
        evidence_ids=("evidence-1",),
    )
    factory.create_lead(
        lead_id="lead-1",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test",
    )
    factory.qualify_lead(qualification_ref="qualification:test")
    factory.draft_proposal(proposal_id="proposal-1", scope_summary="Fixture scope")
    business_authority.allow_once("approval:proposal")
    factory.approve_proposal(approval_ref="approval:proposal")
    business_authority.allow_once("approval:work-order")
    factory.create_work_order(
        work_order_id="work-order-1",
        scope="Fixture authorized scope",
        authorization_ref="approval:work-order",
    )
    return factory


def _legacy_allowed_decision():
    dependency = DependencyAdoption(
        project_id="product-project-1",
        component_id="component-httpx",
        package_name="httpx",
        version="0.28.1",
        source_ref="registry:pypi:httpx:0.28.1",
        provenance_ref="sha256:" + "a" * 64,
        license_expression="BSD-3-Clause",
        license_disposition=LicenseDisposition.APPROVED,
        distribution_obligations=("retain-license-notice",),
        notice_required=False,
        review_ref="review:license:httpx",
    )
    return ProductComplianceGate(review_authority=_ReviewAuthority()).evaluate(
        project_id="product-project-1",
        dependencies=(dependency,),
        obligation_evidence=(
            DistributionObligationEvidence(
                project_id="product-project-1",
                component_id="component-httpx",
                obligation="retain-license-notice",
                fulfillment_ref="artifact:license:httpx",
            ),
        ),
        scope_review_ref="review:scope",
    )


def test_business_delivery_rejects_legacy_project_decision_without_exact_release_grant(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory = _factory(business_authority)
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="product-project-1",
        project_name="Fixture Product",
        spec=ProductProjectSpec(
            goal="Build fixture product",
            desired_outcome="Verified artifact",
            compliance={"business_work_order_ref": "work-order-1"},
        ),
        idempotency_key="fixture-handoff",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:report:1")

    legacy_decision = _legacy_allowed_decision()
    assert legacy_decision.allowed is True
    business_authority.allow_once("approval:delivery")

    # The old project-level decision contains no release id, artifact SHA-256 or
    # packaged-notice digest. It must not authorize delivery of an arbitrary artifact.
    with pytest.raises(
        (BusinessFactoryError, TypeError),
        match="release|PF10|compliance grant",
    ):
        factory.record_delivery(
            delivery_id="delivery-1",
            artifact_ref="artifact:caller-chosen-without-release-binding",
            authorization_ref="approval:delivery",
            compliance=legacy_decision,
        )
