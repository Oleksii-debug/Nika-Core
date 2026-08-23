from __future__ import annotations

import pytest

from nika_core.product_compliance import (
    CompetitorResearchEvidence,
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceError,
    ProductComplianceGate,
)


def _dependency(
    *,
    disposition: LicenseDisposition = LicenseDisposition.APPROVED,
    notice_required: bool = True,
    notice_refs: tuple[str, ...] = ("artifact:notices#component",),
) -> DependencyAdoption:
    return DependencyAdoption(
        project_id="project-1",
        component_id="component-1",
        package_name="example-package",
        version="1.2.3",
        source_ref="registry:pypi:example-package:1.2.3",
        provenance_ref="hash:sha256:fixture",
        license_expression="MIT",
        license_disposition=disposition,
        distribution_obligations=("retain-license",),
        notice_required=notice_required,
        notice_refs=notice_refs,
        review_ref="review:license:1",
    )


def _obligation() -> DistributionObligationEvidence:
    return DistributionObligationEvidence(
        project_id="project-1",
        component_id="component-1",
        obligation="retain-license",
        fulfillment_ref="artifact:notices#component",
    )


def test_release_passes_only_with_complete_approved_provenance_and_obligations() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependencies=(_dependency(),),
        obligation_evidence=(_obligation(),),
        competitor_evidence=(
            CompetitorResearchEvidence(
                project_id="project-1",
                evidence_id="competitor-1",
                source_ref="public:https://example.test",
                provenance_ref="research:public:1",
                permitted_public_evidence=True,
                permission_basis_ref="terms-review:public-source:1",
            ),
        ),
    )
    assert decision.allowed is True
    assert decision.findings == ()
    assert "hash:sha256:fixture" in decision.evidence_refs
    assert "review:license:1" in decision.evidence_refs
    assert "terms-review:public-source:1" in decision.evidence_refs
    ProductComplianceGate().require_release_allowed(decision)


def test_missing_dependency_identity_or_provenance_is_rejected_at_contract_boundary() -> None:
    with pytest.raises(ProductComplianceError, match="version"):
        DependencyAdoption(
            project_id="project-1",
            component_id="component-1",
            package_name="pkg",
            version="",
            source_ref="registry:pkg",
            provenance_ref="hash:1",
            license_expression="MIT",
            license_disposition=LicenseDisposition.APPROVED,
        )
    with pytest.raises(ProductComplianceError, match="provenance"):
        DependencyAdoption(
            project_id="project-1",
            component_id="component-1",
            package_name="pkg",
            version="1.0",
            source_ref="registry:pkg",
            provenance_ref="",
            license_expression="MIT",
            license_disposition=LicenseDisposition.APPROVED,
        )


def test_release_allowing_policy_decisions_require_durable_review_evidence() -> None:
    with pytest.raises(ProductComplianceError, match="authorized review_ref"):
        DependencyAdoption(
            project_id="project-1",
            component_id="component-1",
            package_name="pkg",
            version="1.0",
            source_ref="registry:pkg",
            provenance_ref="hash:1",
            license_expression="MIT",
            license_disposition=LicenseDisposition.APPROVED,
        )

    with pytest.raises(ProductComplianceError, match="permission_basis_ref"):
        CompetitorResearchEvidence(
            project_id="project-1",
            evidence_id="competitor-public-no-policy",
            source_ref="public:https://example.test",
            provenance_ref="research:public:no-policy",
            permitted_public_evidence=True,
        )


@pytest.mark.parametrize(
    ("disposition", "finding"),
    [
        (LicenseDisposition.BLOCKED, "license:blocked:component-1"),
        (LicenseDisposition.REVIEW_REQUIRED, "license:review-required:component-1"),
    ],
)
def test_unacceptable_or_unresolved_license_blocks_release(
    disposition: LicenseDisposition,
    finding: str,
) -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependencies=(_dependency(disposition=disposition),),
        obligation_evidence=(_obligation(),),
    )
    assert decision.allowed is False
    assert finding in decision.findings
    with pytest.raises(ProductComplianceError, match="release blocked"):
        ProductComplianceGate().require_release_allowed(decision)


def test_missing_notice_or_distribution_fulfillment_blocks_release() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependencies=(_dependency(notice_refs=()),),
    )
    assert decision.allowed is False
    assert "notice:missing:component-1" in decision.findings
    assert any(item.startswith("distribution-obligation:unfulfilled") for item in decision.findings)


def test_duplicate_or_orphan_distribution_evidence_blocks_release() -> None:
    duplicate = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependencies=(_dependency(),),
        obligation_evidence=(
            _obligation(),
            DistributionObligationEvidence(
                project_id="project-1",
                component_id="component-1",
                obligation="retain-license",
                fulfillment_ref="artifact:notices#duplicate",
            ),
        ),
    )
    assert duplicate.allowed is False
    assert "duplicate:distribution-obligation:component-1:retain-license" in duplicate.findings

    orphan = ProductComplianceGate().evaluate(
        project_id="project-1",
        obligation_evidence=(
            DistributionObligationEvidence(
                project_id="project-1",
                component_id="undeclared-component",
                obligation="retain-license",
                fulfillment_ref="artifact:notices#orphan",
            ),
        ),
    )
    assert orphan.allowed is False
    assert "orphan:distribution-obligation:undeclared-component:retain-license" in orphan.findings


def test_proprietary_material_access_is_not_copy_permission() -> None:
    evidence = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="competitor-private-1",
        source_ref="proprietary:repo:competitor",
        provenance_ref="research:source:private:1",
        permitted_public_evidence=False,
        proprietary_material=True,
    )
    blocked = ProductComplianceGate().evaluate(
        project_id="project-1",
        competitor_evidence=(evidence,),
    )
    assert blocked.allowed is False
    assert "proprietary-reuse:not-authorized:competitor-private-1" in blocked.findings

    authorized_record = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="licensed-reference-1",
        source_ref="licensed:asset:1",
        provenance_ref="research:licensed:1",
        permitted_public_evidence=False,
        proprietary_material=True,
        legal_basis_ref="legal-basis:license:1",
        reuse_authorization_ref="approval:reuse:1",
    )
    allowed = ProductComplianceGate().evaluate(
        project_id="project-1",
        competitor_evidence=(authorized_record,),
    )
    assert allowed.allowed is True


def test_non_public_unpermissioned_research_and_cross_project_evidence_block() -> None:
    not_permitted = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="competitor-1",
        source_ref="restricted:source:1",
        provenance_ref="research:restricted:1",
        permitted_public_evidence=False,
    )
    wrong_project = CompetitorResearchEvidence(
        project_id="project-2",
        evidence_id="competitor-2",
        source_ref="public:https://example.test/2",
        provenance_ref="research:public:2",
        permitted_public_evidence=True,
        permission_basis_ref="terms-review:public-source:2",
    )
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        competitor_evidence=(not_permitted, wrong_project),
    )
    assert decision.allowed is False
    assert "research-source:not-permitted:competitor-1" in decision.findings
    assert "cross-project:competitor-evidence:competitor-2" in decision.findings
