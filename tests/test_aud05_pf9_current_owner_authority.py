from __future__ import annotations

import hashlib

import pytest

import nika_core.product_release_compliance as release_module
from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactoryError,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
    QAState,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)
from nika_core.product_release_compliance import (
    ProductReleaseComplianceGate,
    ReleaseComplianceGrant,
    ReleaseComplianceSnapshot,
)


class _ReleaseReviewAuthority:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        if project_id != self.project_id:
            return False
        if evidence_ref == f"review:dependency-closure:{project_id}":
            return purpose.startswith("dependency-closure:")
        if evidence_ref == f"review:compliance-scope:{project_id}":
            return purpose.startswith("compliance-scope:")
        return False


def _factory_at_work_order(business_authority) -> BusinessFactory:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-aud05-current",
            goal="Deliver the authorized expense product",
            research_package=ResearchEvidencePackage(
                package_id="research-aud05-current",
                evidence=(
                    EvidenceRef(
                        "evidence-aud05-current",
                        "research:public:aud05-current",
                        "Expense workflow demand",
                    ),
                ),
                research_artifact_ref="research:result:aud05-current",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-aud05-current",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
        approval_authority=business_authority,
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-aud05-current",
        title="Authorized expense product",
        evidence_ids=("evidence-aud05-current",),
    )
    factory.create_lead(
        lead_id="lead-aud05-current",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:aud05-current",
    )
    factory.qualify_lead(qualification_ref="qualification:aud05-current")
    factory.draft_proposal(
        proposal_id="proposal-aud05-current",
        scope_summary="Build the authorized expense product.",
    )
    business_authority.allow_once("approval:proposal:aud05-current")
    factory.approve_proposal(approval_ref="approval:proposal:aud05-current")
    business_authority.allow_once("approval:work-order:aud05-current")
    factory.create_work_order(
        work_order_id="work-order-aud05-current",
        scope="Build the authorized expense product.",
        authorization_ref="approval:work-order:aud05-current",
    )
    return factory


def _authorized_spec(*, goal: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A reviewed release candidate",
        compliance={"business_work_order_ref": "work-order-aud05-current"},
    )


def _release_grant(
    *,
    project_id: str,
    release_id: str,
    artifact_ref: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> ReleaseComplianceGrant:
    notice_file = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notice_file.write_text("AUD05 deterministic empty-inventory proof\n", encoding="utf-8")
    notice_hash = hashlib.sha256(notice_file.read_bytes()).hexdigest()
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())
    snapshot = ReleaseComplianceSnapshot(
        project_id=project_id,
        release_id=release_id,
        project_source_ref="git:aud05-current-source",
        project_source_sha256="a" * 64,
        artifact_ref=artifact_ref,
        artifact_sha256="b" * 64,
        notice_bundle_sha256=notice_hash,
        dependencies=(),
        dependency_closure_ref=f"review:dependency-closure:{project_id}",
        scope_review_ref=f"review:compliance-scope:{project_id}",
    )
    gate = ProductReleaseComplianceGate(
        review_authority=_ReleaseReviewAuthority(project_id)
    )
    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True
    return gate.require_release_allowed(decision, snapshot, bundle_dir=tmp_path)


def test_handoff_rejects_same_work_order_with_substituted_product_spec(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory = _factory_at_work_order(business_authority)

    with pytest.raises(BusinessFactoryError):
        factory.handoff_to_product_factory(
            repository=products,
            project_id="product-aud05-substituted",
            project_name="Substituted product",
            spec=_authorized_spec(
                goal="Build a materially different caller-substituted product"
            ),
            idempotency_key="caller-controlled-key-is-not-authority",
        )

    with pytest.raises(KeyError):
        products.get("product-aud05-substituted")


def test_delivery_rejects_valid_grant_for_different_release_identity(
    tmp_path,
    business_authority,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory = _factory_at_work_order(business_authority)
    factory.handoff_to_product_factory(
        repository=products,
        project_id="product-aud05-release",
        project_name="Release-bound product",
        spec=_authorized_spec(goal="Build the authorized expense product"),
        idempotency_key="ignored-caller-key",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:aud05:passed")
    grant = _release_grant(
        project_id="product-aud05-release",
        release_id="release-a",
        artifact_ref="artifact:aud05:shared",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert grant.allowed is True

    business_authority.allow_once("approval:delivery:release-b")
    with pytest.raises(BusinessFactoryError):
        factory.record_delivery(
            delivery_id="release-b",
            artifact_ref="artifact:aud05:shared",
            authorization_ref="approval:delivery:release-b",
            compliance=grant,
        )
