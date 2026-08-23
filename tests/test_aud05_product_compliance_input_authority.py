from __future__ import annotations

from nika_core.product_compliance import (
    DependencyAdoption,
    LicenseDisposition,
    ProductComplianceGate,
)


def test_empty_compliance_scope_cannot_self_authorize_with_opaque_review_text() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="product-aud05-input-authority",
        scope_review_ref="caller:claims-review-happened",
    )

    # An arbitrary caller-owned string is not evidence that an authorized review exists.
    # PF10 must resolve/bind review authority before it can issue a positive release decision.
    assert decision.allowed is False
    assert "compliance-scope:untrusted-review-authority" in decision.findings


def test_dependency_license_cannot_self_authorize_with_opaque_review_text() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="product-aud05-input-authority",
        dependencies=(
            DependencyAdoption(
                project_id="product-aud05-input-authority",
                component_id="component-aud05",
                package_name="example-package",
                version="1.0.0",
                source_ref="source:example-package",
                provenance_ref="provenance:example-package",
                license_expression="MIT",
                license_disposition=LicenseDisposition.APPROVED,
                review_ref="caller:claims-license-review-happened",
            ),
        ),
    )

    assert decision.allowed is False
    assert "license:untrusted-review-authority:component-aud05" in decision.findings
