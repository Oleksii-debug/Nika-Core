from __future__ import annotations

import re

import pytest

from nika_core.product_compliance import (
    CompetitorResearchEvidence,
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceError,
    ProductComplianceGate,
)

_PROJECT = "project-1"
_CLOSURE_REF = "review:dependency-closure:1"
_SCOPE_REF = "review:compliance-scope:1"
_PROVENANCE = "sha256:" + ("a" * 64)
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
            if not purpose.startswith(prefix):
                continue
            return _FINGERPRINT_RE.fullmatch(purpose.removeprefix(prefix)) is not None
        return False


def _gate(*grants: tuple[str, str, str]) -> ProductComplianceGate:
    return ProductComplianceGate(review_authority=_ReviewAuthority(grants))


def _base_grants(*extra: tuple[str, str, str]) -> tuple[tuple[str, str, str], ...]:
    return (
        (_PROJECT, _CLOSURE_REF, "dependency-closure"),
        (_PROJECT, _SCOPE_REF, "compliance-scope"),
        *extra,
    )


def _dependency(
    *,
    disposition: LicenseDisposition = LicenseDisposition.APPROVED,
    notice_required: bool = True,
    notice_refs: tuple[str, ...] = ("artifact:notices#component",),
) -> DependencyAdoption:
    return DependencyAdoption(
        project_id=_PROJECT,
        component_id="component-1",
        package_name="example-package",
        version="1.2.3",
        source_ref="registry:pypi:example-package:1.2.3",
        provenance_ref=_PROVENANCE,
        license_expression="MIT",
        license_disposition=disposition,
        distribution_obligations=("retain-license",),
        notice_required=notice_required,
        notice_refs=notice_refs,
        review_ref="review:license:1",
    )


def _obligation() -> DistributionObligationEvidence:
    return DistributionObligationEvidence(
        project_id=_PROJECT,
        component_id="component-1",
        obligation="retain-license",
        fulfillment_ref="artifact:notices#component",
    )


def _evaluate_complete(
    gate: ProductComplianceGate,
    *,
    competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
):
    return gate.evaluate(
        project_id=_PROJECT,
        dependencies=(_dependency(),),
        obligation_evidence=(_obligation(),),
        competitor_evidence=competitor_evidence,
        dependency_closure_ref=_CLOSURE_REF,
        scope_review_ref=_SCOPE_REF,
    )


def test_release_passes_only_with_complete_approved_provenance_and_obligations() -> None:
    competitor = CompetitorResearchEvidence(
        project_id=_PROJECT,
        evidence_id="competitor-1",
        source_ref="public:https://example.test",
        provenance_ref="research:public:1",
        permitted_public_evidence=True,
        permission_basis_ref="terms-review:public-source:1",
    )
    grants = _base_grants(
        (_PROJECT, "review:license:1", "license-disposition:component-1"),
        (
            _PROJECT,
            "terms-review:public-source:1",
            "public-source-permission:competitor-1",
        ),
    )
    gate = _gate(*grants)
    decision = _evaluate_complete(gate, competitor_evidence=(competitor,))

    assert decision.allowed is True
    assert decision.findings == ()
    assert _PROVENANCE in decision.evidence_refs
    assert "review:license:1" in decision.evidence_refs
    assert "terms-review:public-source:1" in decision.evidence_refs
    gate.require_release_allowed(
        decision,
        project_id=_PROJECT,
        dependencies=(_dependency(),),
        obligation_evidence=(_obligation(),),
        competitor_evidence=(competitor,),
        dependency_closure_ref=_CLOSURE_REF,
        scope_review_ref=_SCOPE_REF,
    )


def test_missing_dependency_identity_or_provenance_is_rejected_at_contract_boundary() -> None:
    with pytest.raises(ProductComplianceError, match="version"):
        DependencyAdoption(
            project_id=_PROJECT,
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
            project_id=_PROJECT,
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
            project_id=_PROJECT,
            component_id="component-1",
            package_name="pkg",
            version="1.0",
            source_ref="registry:pkg",
            provenance_ref=_PROVENANCE,
            license_expression="MIT",
            license_disposition=LicenseDisposition.APPROVED,
        )

    with pytest.raises(ProductComplianceError, match="permission_basis_ref"):
        CompetitorResearchEvidence(
            project_id=_PROJECT,
            evidence_id="competitor-public-no-policy",
            source_ref="public:https://example.test",
            provenance_ref="research:public:no-policy",
            permitted_public_evidence=True,
        )


def test_default_gate_rejects_opaque_review_authority_strings() -> None:
    scope = ProductComplianceGate().evaluate(
        project_id=_PROJECT,
        dependency_closure_ref="caller:claims-closure-reviewed",
        scope_review_ref="caller:claims-review-happened",
    )
    assert scope.allowed is False
    assert "dependency-closure:untrusted-review-authority" in scope.findings
    assert "compliance-scope:untrusted-review-authority" in scope.findings

    license_decision = ProductComplianceGate().evaluate(
        project_id=_PROJECT,
        dependencies=(_dependency(),),
        obligation_evidence=(_obligation(),),
    )
    assert license_decision.allowed is False
    assert "license:untrusted-review-authority:component-1" in license_decision.findings


def test_review_authority_is_bound_to_exact_project_ref_and_purpose() -> None:
    exact_gate = _gate(
        *_base_grants(
            (_PROJECT, "review:license:1", "license-disposition:component-1"),
        )
    )
    exact = _evaluate_complete(exact_gate)
    assert exact.allowed is True

    wrong_project = _gate(
        *_base_grants(
            ("other-project", "review:license:1", "license-disposition:component-1"),
        )
    )
    decision = _evaluate_complete(wrong_project)
    assert decision.allowed is False
    assert "license:untrusted-review-authority:component-1" in decision.findings

    wrong_purpose = _gate(
        *_base_grants(
            (_PROJECT, "review:license:1", "compliance-scope"),
        )
    )
    decision = _evaluate_complete(wrong_purpose)
    assert decision.allowed is False
    assert "license:untrusted-review-authority:component-1" in decision.findings


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
        project_id=_PROJECT,
        dependencies=(_dependency(disposition=disposition),),
        obligation_evidence=(_obligation(),),
    )
    assert decision.allowed is False
    assert finding in decision.findings
    with pytest.raises(ProductComplianceError, match="release blocked"):
        ProductComplianceGate().require_release_allowed(decision)


def test_missing_notice_or_distribution_fulfillment_blocks_release() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id=_PROJECT,
        dependencies=(_dependency(notice_refs=()),),
    )
    assert decision.allowed is False
    assert "notice:missing:component-1" in decision.findings
    assert any(item.startswith("distribution-obligation:unfulfilled") for item in decision.findings)


def test_duplicate_or_orphan_distribution_evidence_blocks_release() -> None:
    duplicate = ProductComplianceGate().evaluate(
        project_id=_PROJECT,
        dependencies=(_dependency(),),
        obligation_evidence=(
            _obligation(),
            DistributionObligationEvidence(
                project_id=_PROJECT,
                component_id="component-1",
                obligation="retain-license",
                fulfillment_ref="artifact:notices#duplicate",
            ),
        ),
    )
    assert duplicate.allowed is False
    assert "duplicate:distribution-obligation:component-1:retain-license" in duplicate.findings

    orphan = ProductComplianceGate().evaluate(
        project_id=_PROJECT,
        obligation_evidence=(
            DistributionObligationEvidence(
                project_id=_PROJECT,
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
        project_id=_PROJECT,
        evidence_id="competitor-private-1",
        source_ref="proprietary:repo:competitor",
        provenance_ref="research:source:private:1",
        permitted_public_evidence=False,
        proprietary_material=True,
    )
    blocked = ProductComplianceGate().evaluate(
        project_id=_PROJECT,
        competitor_evidence=(evidence,),
    )
    assert blocked.allowed is False
    assert "proprietary-reuse:not-authorized:competitor-private-1" in blocked.findings

    authorized_record = CompetitorResearchEvidence(
        project_id=_PROJECT,
        evidence_id="licensed-reference-1",
        source_ref="licensed:asset:1",
        provenance_ref="research:licensed:1",
        permitted_public_evidence=False,
        proprietary_material=True,
        legal_basis_ref="legal-basis:license:1",
        reuse_authorization_ref="approval:reuse:1",
    )
    gate = _gate(
        *_base_grants(
            (
                _PROJECT,
                "legal-basis:license:1",
                "proprietary-legal-basis:licensed-reference-1",
            ),
            (
                _PROJECT,
                "approval:reuse:1",
                "proprietary-reuse-authorization:licensed-reference-1",
            ),
        )
    )
    allowed = gate.evaluate(
        project_id=_PROJECT,
        competitor_evidence=(authorized_record,),
        dependency_closure_ref=_CLOSURE_REF,
        scope_review_ref=_SCOPE_REF,
    )
    assert allowed.allowed is True


def test_trusted_closure_can_authorize_verified_zero_dependency_inventory() -> None:
    gate = _gate(*_base_grants())
    decision = gate.evaluate(
        project_id=_PROJECT,
        dependency_closure_ref=_CLOSURE_REF,
        scope_review_ref=_SCOPE_REF,
    )
    assert decision.allowed is True


def test_non_public_unpermissioned_research_and_cross_project_evidence_block() -> None:
    not_permitted = CompetitorResearchEvidence(
        project_id=_PROJECT,
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
        project_id=_PROJECT,
        competitor_evidence=(not_permitted, wrong_project),
    )
    assert decision.allowed is False
    assert "research-source:not-permitted:competitor-1" in decision.findings
    assert "cross-project:competitor-evidence:competitor-2" in decision.findings
