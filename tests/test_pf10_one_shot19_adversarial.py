from __future__ import annotations

import pytest

from nika_core.product_compliance import (
    DependencyAdoption,
    LicenseDisposition,
    ProductComplianceError,
    ProductComplianceGate,
)


class _ReviewAuthority:
    """Deterministic trusted-host fixture for exact PF10 purpose families."""

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        if project_id != "project-1":
            return False
        if evidence_ref == "review:dependency-closure":
            return purpose.startswith("dependency-closure:")
        if evidence_ref == "review:compliance-scope":
            return purpose.startswith("compliance-scope:")
        if evidence_ref.startswith("review:license:"):
            component_id = evidence_ref.removeprefix("review:license:")
            return purpose.startswith(f"license-disposition:{component_id}:")
        return False


def _dependency(
    component_id: str,
    *,
    version: str = "1.2.3",
    source_ref: str = "registry:pypi:example-package:1.2.3",
    provenance_ref: str = "sha256:" + "a" * 64,
    license_expression: str = "MIT",
    notice_required: bool = False,
    notice_refs: tuple[str, ...] = (),
) -> DependencyAdoption:
    return DependencyAdoption(
        project_id="project-1",
        component_id=component_id,
        package_name="example-package",
        version=version,
        source_ref=source_ref,
        provenance_ref=provenance_ref,
        license_expression=license_expression,
        license_disposition=LicenseDisposition.APPROVED,
        notice_required=notice_required,
        notice_refs=notice_refs,
        review_ref=f"review:license:{component_id}",
    )


def _evaluate(
    gate: ProductComplianceGate,
    *dependencies: DependencyAdoption,
):
    return gate.evaluate(
        project_id="project-1",
        dependencies=tuple(dependencies),
        dependency_closure_ref="review:dependency-closure",
        scope_review_ref="review:compliance-scope",
    )


def test_duplicate_physical_dependency_cannot_hide_behind_component_aliases() -> None:
    gate = ProductComplianceGate(review_authority=_ReviewAuthority())

    decision = _evaluate(
        gate,
        _dependency("desktop"),
        _dependency("transport"),
    )

    assert decision.allowed is False
    assert "duplicate:dependency-identity:transport" in decision.findings


def test_unknown_license_expression_cannot_be_review_laundered_into_release() -> None:
    gate = ProductComplianceGate(review_authority=_ReviewAuthority())

    decision = _evaluate(gate, _dependency("desktop", license_expression="UNKNOWN"))

    assert decision.allowed is False
    assert "license:unknown:desktop" in decision.findings


def test_mutable_source_and_non_content_provenance_do_not_count_as_exact_source() -> None:
    gate = ProductComplianceGate(review_authority=_ReviewAuthority())

    decision = _evaluate(
        gate,
        _dependency(
            "desktop",
            source_ref="https://example.test/example-package/latest.whl",
            provenance_ref="locator:https://example.test/example-package/latest.whl",
        ),
    )

    assert decision.allowed is False
    assert "dependency-source:mutable:desktop" in decision.findings
    assert "dependency-provenance:not-exact-sha256:desktop" in decision.findings


def test_notice_reference_must_not_be_opaque_positive_evidence() -> None:
    gate = ProductComplianceGate(review_authority=_ReviewAuthority())

    decision = _evaluate(
        gate,
        _dependency(
            "desktop",
            notice_required=True,
            notice_refs=("artifact:notices#section-that-does-not-exist",),
        ),
    )

    # A caller-provided non-empty locator is not proof that the canonical packaging
    # notice generator/verifier emitted the required dependency notice.
    assert decision.allowed is False


def test_notice_evidence_without_notice_obligation_is_orphaned() -> None:
    gate = ProductComplianceGate(review_authority=_ReviewAuthority())

    decision = _evaluate(
        gate,
        _dependency(
            "desktop",
            notice_required=False,
            notice_refs=("artifact:notices#unexpected-section",),
        ),
    )

    assert decision.allowed is False
    assert "orphan:notice-ref:desktop" in decision.findings


def test_dependency_change_invalidates_previous_release_decision() -> None:
    gate = ProductComplianceGate(review_authority=_ReviewAuthority())
    original = _dependency("desktop", version="1.2.3")
    decision = _evaluate(gate, original)
    assert decision.allowed is True

    changed = _dependency(
        "desktop",
        version="1.2.4",
        source_ref="registry:pypi:example-package:1.2.4",
        provenance_ref="sha256:" + "b" * 64,
    )
    with pytest.raises(ProductComplianceError, match="stale PF10 compliance decision"):
        gate.require_release_allowed(
            decision,
            project_id="project-1",
            dependencies=(changed,),
            obligation_evidence=(),
            competitor_evidence=(),
            dependency_closure_ref="review:dependency-closure",
            scope_review_ref="review:compliance-scope",
        )
