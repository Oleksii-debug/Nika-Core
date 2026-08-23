from __future__ import annotations

from nika_core.product_compliance import (
    DependencyAdoption,
    LicenseDisposition,
    ProductComplianceGate,
)


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


def _dependency(
    component_id: str,
    *,
    version: str = "1.2.3",
    source_ref: str = "registry:pypi:example-package:1.2.3",
    provenance_ref: str = "hash:sha256:fixture",
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


def _gate(*component_ids: str) -> ProductComplianceGate:
    return ProductComplianceGate(
        review_authority=_ReviewAuthority(
            tuple(
                (
                    "project-1",
                    f"review:license:{component_id}",
                    f"license-disposition:{component_id}",
                )
                for component_id in component_ids
            )
        )
    )


def test_duplicate_physical_dependency_cannot_hide_behind_component_aliases() -> None:
    decision = _gate("desktop", "transport").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency("desktop"),
            _dependency("transport"),
        ),
    )

    assert decision.allowed is False


def test_unknown_license_expression_cannot_be_review_laundered_into_release() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency("desktop", license_expression="UNKNOWN"),
        ),
    )

    assert decision.allowed is False


def test_mutable_source_and_non_content_provenance_do_not_count_as_exact_source() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency(
                "desktop",
                source_ref="https://example.test/example-package/latest.whl",
                provenance_ref="locator:https://example.test/example-package/latest.whl",
            ),
        ),
    )

    assert decision.allowed is False


def test_notice_reference_must_not_be_opaque_positive_evidence() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency(
                "desktop",
                notice_required=True,
                notice_refs=("artifact:notices#section-that-does-not-exist",),
            ),
        ),
    )

    assert decision.allowed is False


def test_notice_evidence_for_component_without_notice_obligation_is_orphaned() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency(
                "desktop",
                notice_required=False,
                notice_refs=("artifact:notices#unexpected-section",),
            ),
        ),
    )

    assert decision.allowed is False


def test_dependency_change_changes_compliance_decision_identity() -> None:
    gate = _gate("desktop")
    version_one = gate.evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop", version="1.2.3"),),
    )
    version_two = gate.evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop", version="1.2.4"),),
    )

    assert version_one.allowed is True
    assert version_two.allowed is True
    assert version_one != version_two
    assert version_one.evidence_refs != version_two.evidence_refs
