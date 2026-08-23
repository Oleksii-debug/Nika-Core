from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.packaging.notices import notice_section_sha256
from nika_core.product_compliance import (
    CompetitorResearchEvidence,
    DependencyAdoption,
    DependencyNoticeEvidence,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceDecision,
    ProductComplianceError,
    ProductComplianceGate,
    ProductComplianceInventory,
)

_SCOPE_REF = "review:compliance-scope:project-1"
_SOURCE_INTEGRITY = "sha256:" + "1" * 64
_NOTICE_BODY = "Declared license: MIT\n\nFixture notice text."
_NOTICE_SHA256 = notice_section_sha256(_NOTICE_BODY)


class _ReviewAuthority:
    def __init__(self, grants: tuple[tuple[str, str, str], ...]) -> None:
        self.grants = set(grants)

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (project_id, evidence_ref, purpose) in self.grants


class _BrokenReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        raise RuntimeError(f"authority unavailable: {project_id}:{purpose}:{evidence_ref}")


def _gate(*grants: tuple[str, str, str]) -> ProductComplianceGate:
    return ProductComplianceGate(review_authority=_ReviewAuthority(grants))


def _dependency(
    *,
    project_id: str = "project-1",
    component_id: str = "component-1",
    package_name: str = "example-package",
    version: str = "1.2.3",
    source_ref: str = "registry:pypi:example-package:1.2.3",
    source_integrity_ref: str = _SOURCE_INTEGRITY,
    license_expression: str = "MIT",
    disposition: LicenseDisposition = LicenseDisposition.APPROVED,
    obligations: tuple[str, ...] = ("retain-license",),
    notice_required: bool = True,
    notice_refs: tuple[str, ...] = ("artifact:notices#component-1",),
    depends_on_component_ids: tuple[str, ...] = (),
    review_ref: str | None = "review:license:1",
) -> DependencyAdoption:
    return DependencyAdoption(
        project_id=project_id,
        component_id=component_id,
        package_name=package_name,
        version=version,
        source_ref=source_ref,
        source_integrity_ref=source_integrity_ref,
        provenance_ref=f"provenance:{component_id}:{version}",
        license_expression=license_expression,
        license_disposition=disposition,
        distribution_obligations=obligations,
        notice_required=notice_required,
        notice_refs=notice_refs,
        depends_on_component_ids=depends_on_component_ids,
        review_ref=review_ref,
    )


def _obligation(
    *,
    project_id: str = "project-1",
    component_id: str = "component-1",
    obligation: str = "retain-license",
    fulfillment_ref: str = "artifact:notices#component-1",
) -> DistributionObligationEvidence:
    return DistributionObligationEvidence(
        project_id=project_id,
        component_id=component_id,
        obligation=obligation,
        fulfillment_ref=fulfillment_ref,
    )


def _notice(
    *,
    project_id: str = "project-1",
    component_id: str = "component-1",
    notice_ref: str = "artifact:notices#component-1",
    package_name: str = "example-package",
    version: str = "1.2.3",
    section_title: str = "example-package 1.2.3",
    section_sha256: str = _NOTICE_SHA256,
) -> DependencyNoticeEvidence:
    return DependencyNoticeEvidence(
        project_id=project_id,
        component_id=component_id,
        notice_ref=notice_ref,
        package_name=package_name,
        version=version,
        section_title=section_title,
        section_sha256=section_sha256,
    )


def _complete_inventory() -> ProductComplianceInventory:
    return ProductComplianceInventory(
        project_id="project-1",
        dependencies=(_dependency(),),
        obligation_evidence=(_obligation(),),
        notice_evidence=(_notice(),),
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
        scope_review_ref=_SCOPE_REF,
    )


def _complete_gate() -> ProductComplianceGate:
    return _gate(
        ("project-1", _SCOPE_REF, "compliance-scope"),
        ("project-1", "review:license:1", "license-disposition:component-1"),
        (
            "project-1",
            "terms-review:public-source:1",
            "public-source-permission:competitor-1",
        ),
    )


def test_release_passes_only_with_complete_exact_inventory_and_current_authority() -> None:
    gate = _complete_gate()
    inventory = _complete_inventory()
    decision = gate.evaluate_inventory(inventory)

    assert decision.allowed is True
    assert decision.findings == ()
    assert decision.inventory_digest == inventory.digest
    assert _SOURCE_INTEGRITY in decision.evidence_refs
    assert f"compliance-inventory:sha256:{inventory.digest}" in decision.evidence_refs
    gate.require_release_allowed(decision, inventory=inventory)


def test_missing_dependency_identity_provenance_or_integrity_is_rejected() -> None:
    with pytest.raises(ProductComplianceError, match="version"):
        _dependency(version="")
    with pytest.raises(ProductComplianceError, match="provenance"):
        DependencyAdoption(
            project_id="project-1",
            component_id="component-1",
            package_name="pkg",
            version="1.0",
            source_ref="registry:pkg:1.0",
            source_integrity_ref=_SOURCE_INTEGRITY,
            provenance_ref="",
            license_expression="MIT",
            license_disposition=LicenseDisposition.REVIEW_REQUIRED,
        )
    with pytest.raises(ProductComplianceError, match="source_integrity_ref"):
        _dependency(source_integrity_ref="branch:main")


def test_mutable_source_reference_requires_separate_immutable_content_identity() -> None:
    dependency = _dependency(source_ref="https://example.test/releases/latest")
    assert dependency.source_ref.endswith("/latest")
    assert dependency.source_integrity_ref == _SOURCE_INTEGRITY

    with pytest.raises(ProductComplianceError, match="immutable"):
        _dependency(
            source_ref="https://example.test/releases/latest",
            source_integrity_ref="https://example.test/releases/latest",
        )


def test_unknown_license_expression_cannot_be_laundered_by_approved_enum() -> None:
    inventory = replace(
        _complete_inventory(),
        dependencies=(_dependency(license_expression="NOASSERTION"),),
    )
    decision = _complete_gate().evaluate_inventory(inventory)

    assert decision.allowed is False
    assert "license:unresolved-expression:component-1" in decision.findings


def test_review_required_or_blocked_license_remains_release_blocking() -> None:
    for disposition, finding in (
        (LicenseDisposition.REVIEW_REQUIRED, "license:review-required:component-1"),
        (LicenseDisposition.BLOCKED, "license:blocked:component-1"),
    ):
        inventory = replace(
            _complete_inventory(),
            dependencies=(_dependency(disposition=disposition),),
        )
        decision = _complete_gate().evaluate_inventory(inventory)
        assert decision.allowed is False
        assert finding in decision.findings


def test_approved_license_requires_review_evidence_at_contract_boundary() -> None:
    with pytest.raises(ProductComplianceError, match="authorized review_ref"):
        _dependency(review_ref=None)


def test_caller_fabricated_review_refs_and_broken_authority_fail_closed() -> None:
    inventory = _complete_inventory()
    opaque = ProductComplianceGate().evaluate_inventory(inventory)
    assert opaque.allowed is False
    assert "compliance-scope:untrusted-review-authority" in opaque.findings
    assert "license:untrusted-review-authority:component-1" in opaque.findings

    broken = ProductComplianceGate(
        review_authority=_BrokenReviewAuthority()
    ).evaluate_inventory(inventory)
    assert broken.allowed is False
    assert "compliance-scope:untrusted-review-authority" in broken.findings


def test_review_authority_is_bound_to_exact_project_ref_and_purpose() -> None:
    inventory = _complete_inventory()
    wrong_project = _gate(
        ("other-project", _SCOPE_REF, "compliance-scope"),
        ("other-project", "review:license:1", "license-disposition:component-1"),
    ).evaluate_inventory(inventory)
    assert wrong_project.allowed is False
    assert "compliance-scope:untrusted-review-authority" in wrong_project.findings
    assert "license:untrusted-review-authority:component-1" in wrong_project.findings

    wrong_purpose = _gate(
        ("project-1", _SCOPE_REF, "license-disposition:component-1"),
        ("project-1", "review:license:1", "compliance-scope"),
    ).evaluate_inventory(inventory)
    assert wrong_purpose.allowed is False
    assert "compliance-scope:untrusted-review-authority" in wrong_purpose.findings


def test_duplicate_exact_dependency_identity_blocks_even_under_different_component_ids() -> None:
    duplicate = _dependency(
        component_id="component-duplicate",
        notice_refs=("artifact:notices#duplicate",),
        obligations=(),
        notice_required=False,
        review_ref="review:license:duplicate",
    )
    inventory = replace(
        _complete_inventory(),
        dependencies=(_dependency(), duplicate),
    )
    gate = _gate(
        ("project-1", _SCOPE_REF, "compliance-scope"),
        ("project-1", "review:license:1", "license-disposition:component-1"),
        (
            "project-1",
            "review:license:duplicate",
            "license-disposition:component-duplicate",
        ),
        (
            "project-1",
            "terms-review:public-source:1",
            "public-source-permission:competitor-1",
        ),
    )
    decision = gate.evaluate_inventory(inventory)

    assert decision.allowed is False
    assert "duplicate:dependency-identity:component-1:component-duplicate" in decision.findings


def test_missing_transitive_dependency_inventory_blocks_release() -> None:
    root = _dependency(depends_on_component_ids=("component-transitive",))
    inventory = replace(_complete_inventory(), dependencies=(root,))
    decision = _complete_gate().evaluate_inventory(inventory)

    assert decision.allowed is False
    assert "dependency:missing-transitive:component-1:component-transitive" in decision.findings


def test_transitive_dependency_obligations_are_not_inherited_away() -> None:
    root = _dependency(depends_on_component_ids=("component-transitive",))
    transitive = _dependency(
        component_id="component-transitive",
        package_name="transitive-package",
        version="2.0.0",
        source_ref="registry:pypi:transitive-package:2.0.0",
        source_integrity_ref="sha256:" + "2" * 64,
        obligations=("retain-transitive-license",),
        notice_required=False,
        notice_refs=(),
        review_ref="review:license:transitive",
    )
    inventory = replace(
        _complete_inventory(),
        dependencies=(root, transitive),
    )
    gate = _gate(
        ("project-1", _SCOPE_REF, "compliance-scope"),
        ("project-1", "review:license:1", "license-disposition:component-1"),
        (
            "project-1",
            "review:license:transitive",
            "license-disposition:component-transitive",
        ),
        (
            "project-1",
            "terms-review:public-source:1",
            "public-source-permission:competitor-1",
        ),
    )
    decision = gate.evaluate_inventory(inventory)

    assert decision.allowed is False
    assert (
        "distribution-obligation:unfulfilled:component-transitive:retain-transitive-license"
        in decision.findings
    )


def test_missing_duplicate_or_orphan_notice_evidence_blocks_release() -> None:
    missing = replace(_complete_inventory(), notice_evidence=())
    missing_decision = _complete_gate().evaluate_inventory(missing)
    assert missing_decision.allowed is False
    assert (
        "notice:evidence-missing:component-1:artifact:notices#component-1"
        in missing_decision.findings
    )

    duplicate_notice = _notice()
    duplicate = replace(
        _complete_inventory(),
        notice_evidence=(_notice(), duplicate_notice),
    )
    duplicate_decision = _complete_gate().evaluate_inventory(duplicate)
    assert duplicate_decision.allowed is False
    assert "duplicate:notice-ref:artifact:notices#component-1" in duplicate_decision.findings

    orphan = replace(
        _complete_inventory(),
        notice_evidence=(
            _notice(),
            _notice(
                component_id="undeclared-component",
                notice_ref="artifact:notices#orphan",
            ),
        ),
    )
    orphan_decision = _complete_gate().evaluate_inventory(orphan)
    assert orphan_decision.allowed is False
    assert "orphan:notice:undeclared-component:artifact:notices#orphan" in (
        orphan_decision.findings
    )


def test_duplicate_or_orphan_distribution_evidence_blocks_release() -> None:
    duplicate = replace(
        _complete_inventory(),
        obligation_evidence=(
            _obligation(),
            _obligation(fulfillment_ref="artifact:notices#duplicate"),
        ),
    )
    duplicate_decision = _complete_gate().evaluate_inventory(duplicate)
    assert duplicate_decision.allowed is False
    assert "duplicate:distribution-obligation:component-1:retain-license" in (
        duplicate_decision.findings
    )

    orphan = replace(
        _complete_inventory(),
        obligation_evidence=(
            _obligation(),
            _obligation(component_id="undeclared-component"),
        ),
    )
    orphan_decision = _complete_gate().evaluate_inventory(orphan)
    assert orphan_decision.allowed is False
    assert "orphan:distribution-obligation:undeclared-component:retain-license" in (
        orphan_decision.findings
    )


def test_packaged_notice_binding_uses_canonical_notice_parser_and_exact_digest(
    tmp_path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    notices = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    notices.write_text(
        "Nika Core third-party notices\n\n"
        "===== example-package 1.2.3 =====\n"
        f"{_NOTICE_BODY}\n",
        encoding="utf-8",
    )
    gate = _complete_gate()
    inventory = _complete_inventory()
    decision = gate.evaluate_inventory(inventory)
    gate.require_packaged_release_allowed(
        decision,
        inventory=inventory,
        bundle_dir=bundle_dir,
    )

    notices.write_text(
        "Nika Core third-party notices\n\n"
        "===== example-package 1.2.3 =====\n"
        "tampered notice\n",
        encoding="utf-8",
    )
    with pytest.raises(ProductComplianceError, match="notice:packaging"):
        gate.require_packaged_release_allowed(
            decision,
            inventory=inventory,
            bundle_dir=bundle_dir,
        )


def test_proprietary_material_access_is_not_copy_permission_or_public_evidence() -> None:
    with pytest.raises(ProductComplianceError, match="both proprietary material and public"):
        CompetitorResearchEvidence(
            project_id="project-1",
            evidence_id="laundered",
            source_ref="proprietary:repo:competitor",
            provenance_ref="research:source:private:1",
            permitted_public_evidence=True,
            proprietary_material=True,
            permission_basis_ref="terms:public",
        )

    evidence = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="competitor-private-1",
        source_ref="proprietary:repo:competitor",
        provenance_ref="research:source:private:1",
        permitted_public_evidence=False,
        proprietary_material=True,
    )
    inventory = replace(
        _complete_inventory(),
        competitor_evidence=(evidence,),
    )
    blocked = _complete_gate().evaluate_inventory(inventory)
    assert blocked.allowed is False
    assert "proprietary-reuse:not-authorized:competitor-private-1" in blocked.findings


def test_proprietary_reuse_authority_is_project_and_purpose_bound() -> None:
    evidence = CompetitorResearchEvidence(
        project_id="project-1",
        evidence_id="licensed-reference-1",
        source_ref="licensed:asset:1",
        provenance_ref="research:licensed:1",
        permitted_public_evidence=False,
        proprietary_material=True,
        legal_basis_ref="legal-basis:license:1",
        reuse_authorization_ref="approval:reuse:1",
    )
    inventory = replace(_complete_inventory(), competitor_evidence=(evidence,))
    wrong_project = _gate(
        ("project-1", _SCOPE_REF, "compliance-scope"),
        ("project-1", "review:license:1", "license-disposition:component-1"),
        (
            "other-project",
            "legal-basis:license:1",
            "proprietary-legal-basis:licensed-reference-1",
        ),
        (
            "other-project",
            "approval:reuse:1",
            "proprietary-reuse-authorization:licensed-reference-1",
        ),
    ).evaluate_inventory(inventory)
    assert wrong_project.allowed is False
    assert "proprietary-reuse:untrusted-authority:licensed-reference-1" in (
        wrong_project.findings
    )


def test_stale_decision_after_dependency_change_is_rejected() -> None:
    gate = _complete_gate()
    original = _complete_inventory()
    decision = gate.evaluate_inventory(original)
    assert decision.allowed is True

    changed_dependency = _dependency(
        version="1.2.4",
        source_ref="registry:pypi:example-package:1.2.4",
        source_integrity_ref="sha256:" + "4" * 64,
    )
    changed_notice = _notice(
        version="1.2.4",
        section_title="example-package 1.2.4",
    )
    changed = replace(
        original,
        dependencies=(changed_dependency,),
        notice_evidence=(changed_notice,),
    )
    assert changed.digest != original.digest

    with pytest.raises(ProductComplianceError, match="stale-inventory"):
        gate.require_release_allowed(decision, inventory=changed)


def test_release_revalidates_review_authority_instead_of_replaying_old_grant() -> None:
    authority = _ReviewAuthority(
        (
            ("project-1", _SCOPE_REF, "compliance-scope"),
            ("project-1", "review:license:1", "license-disposition:component-1"),
            (
                "project-1",
                "terms-review:public-source:1",
                "public-source-permission:competitor-1",
            ),
        )
    )
    gate = ProductComplianceGate(review_authority=authority)
    inventory = _complete_inventory()
    decision = gate.evaluate_inventory(inventory)
    assert decision.allowed is True

    authority.grants.clear()
    with pytest.raises(ProductComplianceError, match="authority revalidation"):
        gate.require_release_allowed(decision, inventory=inventory)


def test_decision_cross_project_replay_and_tamper_are_rejected() -> None:
    gate = _complete_gate()
    inventory = _complete_inventory()
    decision = gate.evaluate_inventory(inventory)
    assert decision.allowed is True

    wrong_project_inventory = replace(inventory, project_id="project-2")
    with pytest.raises(ProductComplianceError, match="cross-project-replay"):
        gate.require_release_allowed(decision, inventory=wrong_project_inventory)

    tampered = replace(decision, evidence_refs=("attacker:evidence",))
    assert tampered.allowed is False
    with pytest.raises(ProductComplianceError, match="untrusted-origin"):
        gate.require_release_allowed(tampered, inventory=inventory)


def test_persisted_public_decision_fields_do_not_survive_as_release_authority() -> None:
    gate = _complete_gate()
    inventory = _complete_inventory()
    decision = gate.evaluate_inventory(inventory)
    assert decision.allowed is True

    reconstructed = ProductComplianceDecision(
        project_id=decision.project_id,
        allowed=True,
        findings=decision.findings,
        evidence_refs=decision.evidence_refs,
        inventory_digest=decision.inventory_digest,
    )
    assert reconstructed.allowed is False
    with pytest.raises(ProductComplianceError, match="untrusted-origin"):
        gate.require_release_allowed(reconstructed, inventory=inventory)


def test_release_requires_current_inventory_even_for_gate_issued_decision() -> None:
    gate = _complete_gate()
    decision = gate.evaluate_inventory(_complete_inventory())
    assert decision.allowed is True

    with pytest.raises(ProductComplianceError, match="current-inventory-required"):
        gate.require_release_allowed(decision)
