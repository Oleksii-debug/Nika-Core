from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_compliance import (
    CompetitorResearchEvidence,
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    PackagedDependencyEvidence,
    ProductComplianceError,
    ProductComplianceGate,
    ProductComplianceSnapshot,
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


class _PackagingAuthority:
    def __init__(
        self,
        project_id: str,
        inventory: tuple[PackagedDependencyEvidence, ...],
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


def _dependency(
    *,
    component_id: str = "component-1",
    package_name: str = "example-package",
    version: str = "1.2.3",
    source_ref: str | None = None,
    source_sha256: str = SOURCE_SHA,
    provenance_ref: str = "provenance:source-record:1",
    license_expression: str = "MIT",
    disposition: LicenseDisposition = LicenseDisposition.APPROVED,
    obligations: tuple[str, ...] = ("retain-license",),
    notice_required: bool = True,
    notice_refs: tuple[str, ...] | None = None,
    review_ref: str | None = None,
    project_id: str = "project-1",
) -> DependencyAdoption:
    if source_ref is None:
        source_ref = f"registry:pypi:{package_name}:{version}"
    if notice_refs is None:
        notice_refs = (
            (f"artifact:THIRD_PARTY_NOTICES.txt#{package_name}@{version}",)
            if notice_required
            else ()
        )
    if review_ref is None and disposition is LicenseDisposition.APPROVED:
        review_ref = f"review:license:{component_id}"
    return DependencyAdoption(
        project_id=project_id,
        component_id=component_id,
        package_name=package_name,
        version=version,
        source_ref=source_ref,
        source_sha256=source_sha256,
        provenance_ref=provenance_ref,
        license_expression=license_expression,
        license_disposition=disposition,
        distribution_obligations=obligations,
        notice_required=notice_required,
        notice_refs=notice_refs,
        review_ref=review_ref,
    )


def _obligation(
    dependency: DependencyAdoption,
    *,
    fulfillment_ref: str | None = None,
) -> DistributionObligationEvidence:
    return DistributionObligationEvidence(
        project_id=dependency.project_id,
        component_id=dependency.component_id,
        obligation=dependency.distribution_obligations[0],
        fulfillment_ref=fulfillment_ref or dependency.notice_refs[0],
    )


def _package(dependency: DependencyAdoption) -> PackagedDependencyEvidence:
    notice_ref = (
        dependency.notice_refs[0]
        if dependency.notice_refs
        else f"artifact:THIRD_PARTY_NOTICES.txt#{dependency.package_name}@{dependency.version}"
    )
    return PackagedDependencyEvidence(
        package_name=dependency.package_name,
        version=dependency.version,
        notice_ref=notice_ref,
        notice_sha256=NOTICE_SHA,
    )


def _reviewed_gate(
    *,
    project_id: str = "project-1",
    dependencies: tuple[DependencyAdoption, ...] = (),
    obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
    competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
    packaged_dependencies: tuple[PackagedDependencyEvidence, ...] | None = None,
    scope_review_ref: str | None = None,
    extra_grants: tuple[tuple[str, str, str], ...] = (),
) -> ProductComplianceGate:
    if packaged_dependencies is None:
        packaged_dependencies = tuple(_package(item) for item in dependencies)
    packaging = _PackagingAuthority(project_id, packaged_dependencies)
    grants: list[tuple[str, str, str]] = list(extra_grants)
    for dependency in dependencies:
        if dependency.review_ref is not None:
            grants.append(
                (
                    project_id,
                    dependency.review_ref,
                    dependency.license_review_purpose,
                )
            )
    for evidence in competitor_evidence:
        if evidence.permission_basis_ref:
            grants.append(
                (
                    project_id,
                    evidence.permission_basis_ref,
                    evidence.public_permission_purpose,
                )
            )
        if evidence.legal_basis_ref:
            grants.append(
                (
                    project_id,
                    evidence.legal_basis_ref,
                    evidence.proprietary_legal_purpose,
                )
            )
        if evidence.reuse_authorization_ref:
            grants.append(
                (
                    project_id,
                    evidence.reuse_authorization_ref,
                    evidence.proprietary_reuse_purpose,
                )
            )
    if scope_review_ref is not None:
        snapshot = ProductComplianceSnapshot(
            project_id=project_id,
            dependencies=dependencies,
            obligation_evidence=obligation_evidence,
            competitor_evidence=competitor_evidence,
            packaged_dependencies=packaged_dependencies,
            scope_review_ref=scope_review_ref,
        )
        grants.append((project_id, scope_review_ref, snapshot.scope_review_purpose))
    return ProductComplianceGate(
        review_authority=_ReviewAuthority(tuple(grants)),
        packaging_authority=packaging,
    )


def test_release_passes_only_with_complete_exact_evidence() -> None:
    dependency = _dependency()
    obligation = _obligation(dependency)
    competitor = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="competitor-1",
        source_ref="public:https://example.test",
        provenance_ref="research:public:1",
        permitted_public_evidence=True,
        permission_basis_ref="terms-review:public-source:1",
    )
    gate = _reviewed_gate(
        dependencies=(dependency,),
        obligation_evidence=(obligation,),
        competitor_evidence=(competitor,),
    )
    decision = gate.evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(obligation,),
        competitor_evidence=(competitor,),
    )
    assert decision.allowed is True
    assert decision.findings == ()
    assert f"source-sha256:{SOURCE_SHA}" in decision.evidence_refs
    assert any(item.startswith("compliance-snapshot:sha256:") for item in decision.evidence_refs)
    current = gate.snapshot(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(obligation,),
        competitor_evidence=(competitor,),
    )
    gate.require_release_allowed(decision, current_snapshot=current)


def test_missing_dependency_identity_provenance_or_exact_digest_is_rejected() -> None:
    with pytest.raises(ProductComplianceError, match="version"):
        _dependency(version="")
    with pytest.raises(ProductComplianceError, match="provenance"):
        _dependency(provenance_ref="")
    with pytest.raises(ProductComplianceError, match="source_sha256"):
        _dependency(source_sha256="not-a-sha256")


def test_unknown_or_unasserted_license_expression_is_not_review_launderable() -> None:
    for expression in ("UNKNOWN", "NOASSERTION"):
        with pytest.raises(ProductComplianceError, match="unknown or unasserted"):
            _dependency(license_expression=expression)


def test_release_allowing_policy_requires_review_evidence() -> None:
    with pytest.raises(ProductComplianceError, match="authorized review_ref"):
        _dependency(review_ref=None, disposition=LicenseDisposition.APPROVED)

    with pytest.raises(ProductComplianceError, match="permission_basis_ref"):
        CompetitorResearchEvidence(
            project_id="project-1",
            evidence_id="competitor-public-no-policy",
            source_ref="public:https://example.test",
            provenance_ref="research:public:no-policy",
            permitted_public_evidence=True,
        )


def test_default_gate_fails_closed_without_review_or_packaging_authority() -> None:
    dependency = _dependency()
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(_obligation(dependency),),
    )
    assert decision.allowed is False
    assert "packaging:untrusted-inventory-authority" in decision.findings
    assert f"license:untrusted-review-authority:{dependency.component_id}" in decision.findings


def test_review_authority_is_bound_to_exact_project_ref_purpose_and_dependency() -> None:
    dependency = _dependency()
    packaging = _PackagingAuthority("project-1", (_package(dependency),))
    exact = ProductComplianceGate(
        review_authority=_ReviewAuthority(
            (("project-1", dependency.review_ref or "", dependency.license_review_purpose),)
        ),
        packaging_authority=packaging,
    ).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(_obligation(dependency),),
    )
    assert exact.allowed is True

    changed = replace(dependency, version="1.2.4")
    stale_review = ProductComplianceGate(
        review_authority=_ReviewAuthority(
            (("project-1", dependency.review_ref or "", dependency.license_review_purpose),)
        ),
        packaging_authority=_PackagingAuthority("project-1", (_package(changed),)),
    ).evaluate(
        project_id="project-1",
        dependencies=(changed,),
        obligation_evidence=(_obligation(changed),),
    )
    assert stale_review.allowed is False
    assert f"license:untrusted-review-authority:{changed.component_id}" in stale_review.findings


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
    dependency = _dependency(disposition=disposition, review_ref="review:nonpositive")
    decision = _reviewed_gate(dependencies=(dependency,)).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(_obligation(dependency),),
    )
    assert decision.allowed is False
    assert finding in decision.findings


def test_missing_notice_or_distribution_fulfillment_blocks_release() -> None:
    dependency = _dependency(notice_refs=())
    decision = _reviewed_gate(dependencies=(dependency,)).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
    )
    assert decision.allowed is False
    assert f"notice:missing:{dependency.component_id}" in decision.findings
    assert any(item.startswith("distribution-obligation:unfulfilled") for item in decision.findings)


def test_duplicate_or_orphan_distribution_evidence_blocks_release() -> None:
    dependency = _dependency()
    obligation = _obligation(dependency)
    duplicate = _reviewed_gate(dependencies=(dependency,)).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(
            obligation,
            replace(obligation, fulfillment_ref="artifact:duplicate"),
        ),
    )
    assert "duplicate:distribution-obligation:component-1:retain-license" in duplicate.findings

    orphan = _reviewed_gate(scope_review_ref="review:scope:empty").evaluate(
        project_id="project-1",
        obligation_evidence=(
            DistributionObligationEvidence(
                project_id="project-1",
                component_id="undeclared-component",
                obligation="retain-license",
                fulfillment_ref="artifact:orphan",
            ),
        ),
        scope_review_ref="review:scope:empty",
    )
    assert "orphan:distribution-obligation:undeclared-component:retain-license" in orphan.findings


def test_proprietary_material_requires_exact_legal_and_reuse_authority() -> None:
    evidence = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="competitor-private-1",
        source_ref="proprietary:repo:competitor",
        provenance_ref="research:source:private:1",
        permitted_public_evidence=False,
        proprietary_material=True,
    )
    blocked = _reviewed_gate(scope_review_ref="review:scope:private").evaluate(
        project_id="project-1",
        competitor_evidence=(evidence,),
        scope_review_ref="review:scope:private",
    )
    assert "proprietary-reuse:not-authorized:competitor-private-1" in blocked.findings

    authorized = replace(
        evidence,
        evidence_id="licensed-reference-1",
        legal_basis_ref="legal-basis:license:1",
        reuse_authorization_ref="approval:reuse:1",
    )
    allowed = _reviewed_gate(competitor_evidence=(authorized,)).evaluate(
        project_id="project-1",
        competitor_evidence=(authorized,),
    )
    assert allowed.allowed is True


def test_non_public_unpermissioned_and_cross_project_evidence_block() -> None:
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
    decision = _reviewed_gate(scope_review_ref="review:scope:research").evaluate(
        project_id="project-1",
        competitor_evidence=(not_permitted, wrong_project),
        scope_review_ref="review:scope:research",
    )
    assert "research-source:not-permitted:competitor-1" in decision.findings
    assert "cross-project:competitor-evidence:competitor-2" in decision.findings


def test_duplicate_physical_dependency_cannot_hide_behind_component_aliases() -> None:
    first = _dependency(component_id="desktop", review_ref="review:desktop")
    second = _dependency(component_id="transport", review_ref="review:transport")
    decision = _reviewed_gate(
        dependencies=(first, second),
        packaged_dependencies=(_package(first),),
    ).evaluate(
        project_id="project-1",
        dependencies=(first, second),
        obligation_evidence=(_obligation(first), _obligation(second)),
    )
    assert decision.allowed is False
    assert any(item.startswith("duplicate:physical-dependency:") for item in decision.findings)


def test_opaque_notice_reference_cannot_authorize_generated_notices() -> None:
    dependency = _dependency(
        notice_refs=("artifact:THIRD_PARTY_NOTICES.txt#does-not-exist",),
    )
    package = PackagedDependencyEvidence(
        package_name=dependency.package_name,
        version=dependency.version,
        notice_ref="artifact:THIRD_PARTY_NOTICES.txt#example-package@1.2.3",
        notice_sha256=NOTICE_SHA,
    )
    decision = _reviewed_gate(
        dependencies=(dependency,),
        packaged_dependencies=(package,),
    ).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(_obligation(dependency),),
    )
    assert decision.allowed is False
    assert f"notice:mismatch:{dependency.component_id}" in decision.findings


def test_notice_reference_without_notice_obligation_is_rejected_at_boundary() -> None:
    with pytest.raises(ProductComplianceError, match="orphaned"):
        _dependency(
            notice_required=False,
            notice_refs=("artifact:THIRD_PARTY_NOTICES.txt#unexpected",),
        )


def test_packaging_inventory_catches_undeclared_transitive_dependency() -> None:
    dependency = _dependency()
    transitive = PackagedDependencyEvidence(
        package_name="transitive-helper",
        version="4.5.6",
        notice_ref="artifact:THIRD_PARTY_NOTICES.txt#transitive-helper@4.5.6",
        notice_sha256="3" * 64,
    )
    decision = _reviewed_gate(
        dependencies=(dependency,),
        packaged_dependencies=(_package(dependency), transitive),
    ).evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(_obligation(dependency),),
    )
    assert decision.allowed is False
    assert "packaging:undeclared-dependency:transitive-helper:4.5.6" in decision.findings


def test_dependency_change_invalidates_decision_snapshot_and_release_replay() -> None:
    first = _dependency(version="1.2.3")
    first_obligation = _obligation(first)
    first_gate = _reviewed_gate(
        dependencies=(first,),
        obligation_evidence=(first_obligation,),
    )
    first_decision = first_gate.evaluate(
        project_id="project-1",
        dependencies=(first,),
        obligation_evidence=(first_obligation,),
    )
    assert first_decision.allowed is True

    second = _dependency(version="1.2.4")
    second_obligation = _obligation(second)
    second_gate = _reviewed_gate(
        dependencies=(second,),
        obligation_evidence=(second_obligation,),
    )
    second_decision = second_gate.evaluate(
        project_id="project-1",
        dependencies=(second,),
        obligation_evidence=(second_obligation,),
    )
    assert second_decision.allowed is True
    assert first_decision.snapshot_fingerprint != second_decision.snapshot_fingerprint
    assert first_decision.evidence_refs != second_decision.evidence_refs

    current = second_gate.snapshot(
        project_id="project-1",
        dependencies=(second,),
        obligation_evidence=(second_obligation,),
    )
    with pytest.raises(ProductComplianceError, match="stale"):
        second_gate.require_release_allowed(first_decision, current_snapshot=current)
    second_gate.require_release_allowed(second_decision, current_snapshot=current)


def test_positive_decision_tamper_invalidates_authority() -> None:
    dependency = _dependency()
    obligation = _obligation(dependency)
    gate = _reviewed_gate(dependencies=(dependency,), obligation_evidence=(obligation,))
    decision = gate.evaluate(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(obligation,),
    )
    assert decision.allowed is True

    assert replace(decision, project_id="project-2").allowed is False
    assert replace(decision, snapshot_fingerprint="f" * 64).allowed is False
    assert replace(decision, evidence_refs=("attacker:evidence",)).allowed is False
