from __future__ import annotations

import hashlib
from pathlib import Path

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
from nika_core.product_compliance import DependencyAdoption, LicenseDisposition
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
    ReleaseDependency,
)

_PROJECT_ID = "product-release-identity"
_ARTIFACT_REF = "artifact:candidate:same-content"
_CLOSURE_REF = "review:dependency-closure:release-identity"
_SCOPE_REF = "review:compliance-scope:release-identity"
_LICENSE_REF = "review:license:release-identity"


class _BusinessAuthority:
    def authorize(self, *, intent, evidence_ref: str) -> bool:
        del intent
        return evidence_ref.startswith("approval:test:")


class _ReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        if project_id != _PROJECT_ID:
            return False
        if evidence_ref == _CLOSURE_REF:
            return purpose.startswith("dependency-closure:")
        if evidence_ref == _SCOPE_REF:
            return purpose.startswith("compliance-scope:")
        if evidence_ref == _LICENSE_REF:
            return purpose.startswith("license-disposition:component-runtime:")
        return False


def _factory(tmp_path: Path) -> BusinessFactory:
    authority = _BusinessAuthority()
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-release-identity",
            goal="Exercise exact release-to-delivery identity",
            research_package=ResearchEvidencePackage(
                package_id="research-release-identity",
                evidence=(
                    EvidenceRef(
                        "evidence-release-identity",
                        "research:controlled:release-identity",
                        "Controlled fixture evidence",
                    ),
                ),
                research_artifact_ref="research:artifact:release-identity",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-release-identity",
            allowed_channel_ids=("sandbox",),
            communication_authority=CommunicationAuthority.DRAFT_ONLY,
        ),
        approval_authority=authority,
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-release-identity",
        title="Controlled release identity fixture",
        evidence_ids=("evidence-release-identity",),
    )
    factory.create_lead(
        lead_id="lead-release-identity",
        channel_id="sandbox",
        counterparty_ref="counterparty:fixture",
    )
    factory.qualify_lead(qualification_ref="qualification:fixture")
    factory.draft_proposal(
        proposal_id="proposal-release-identity",
        scope_summary="Controlled release identity scope",
    )
    factory.approve_proposal(approval_ref="approval:test:proposal")
    factory.create_work_order(
        work_order_id="work-order-release-identity",
        scope="Build the controlled release identity fixture",
        authorization_ref="approval:test:work-order",
    )

    store = SQLiteStore(tmp_path / "release-identity.sqlite")
    store.initialize()
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id=_PROJECT_ID,
        project_name="Release Identity Fixture",
        spec=ProductProjectSpec(
            goal="Build release identity fixture",
            desired_outcome="Exact release/delivery binding",
            compliance={"business_work_order_ref": "work-order-release-identity"},
        ),
        idempotency_key="handoff:release-identity",
    )
    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:release-identity:passed")
    return factory


def _grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    release_id: str,
) -> ReleaseComplianceGrant:
    notices = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("release identity fixture notices\n", encoding="utf-8")
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())
    dependency = DependencyAdoption(
        project_id=_PROJECT_ID,
        component_id="component-runtime",
        package_name="example-runtime",
        version="1.0.0",
        source_ref="registry:pypi:example-runtime:1.0.0",
        provenance_ref="hash:sha256:" + "a" * 64,
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        review_ref=_LICENSE_REF,
    )
    snapshot = ReleaseComplianceSnapshot(
        project_id=_PROJECT_ID,
        release_id=release_id,
        project_source_ref="git:fixture@exact",
        project_source_sha256="b" * 64,
        artifact_ref=_ARTIFACT_REF,
        artifact_sha256="c" * 64,
        notice_bundle_sha256=hashlib.sha256(notices.read_bytes()).hexdigest(),
        dependencies=(ReleaseDependency(adoption=dependency, source_sha256="d" * 64),),
        dependency_closure_ref=_CLOSURE_REF,
        scope_review_ref=_SCOPE_REF,
    )
    gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())
    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True
    grant = gate.require_release_allowed(decision, snapshot, bundle_dir=tmp_path)
    assert grant.allowed is True
    return grant


def test_business_delivery_rejects_valid_grant_replayed_under_different_release_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory(tmp_path)
    grant = _grant(tmp_path, monkeypatch, release_id="release-A")

    with pytest.raises(BusinessFactoryError, match="release compliance grant"):
        factory.record_delivery(
            delivery_id="release-B",
            artifact_ref=_ARTIFACT_REF,
            authorization_ref="approval:test:delivery",
            compliance=grant,
        )
