from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_compliance import (
    ProductComplianceDecision,
    ProductComplianceError,
    ProductComplianceGate,
)


def test_caller_constructed_positive_decision_has_no_release_authority() -> None:
    forged = ProductComplianceDecision(
        project_id="project-1",
        allowed=True,
        findings=(),
        evidence_refs=("caller:asserted-compliance",),
    )

    assert forged.allowed is False
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(forged)


def test_gate_issued_positive_decision_has_process_local_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        scope_review_ref="review:compliance-scope:project-1",
    )

    assert decision.allowed is True
    assert decision.findings == ()
    assert "review:compliance-scope:project-1" in decision.evidence_refs
    ProductComplianceGate().require_release_allowed(decision)


def test_positive_decision_tamper_invalidates_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-1",
        scope_review_ref="review:compliance-scope:project-1",
    )
    assert decision.allowed is True

    project_substitution = replace(decision, project_id="project-2")
    evidence_substitution = replace(
        decision,
        evidence_refs=("review:attacker-substitution",),
    )

    assert project_substitution.allowed is False
    assert evidence_substitution.allowed is False
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(project_substitution)
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(evidence_substitution)


def test_missing_compliance_scope_review_blocks_empty_inventory_false_green() -> None:
    unreviewed = ProductComplianceGate().evaluate(project_id="project-1")

    assert unreviewed.allowed is False
    assert "compliance-scope:unreviewed" in unreviewed.findings
    with pytest.raises(ProductComplianceError, match="compliance-scope:unreviewed"):
        ProductComplianceGate().require_release_allowed(unreviewed)


def test_explicit_review_can_authorize_legitimately_empty_compliance_inventory() -> None:
    reviewed = ProductComplianceGate().evaluate(
        project_id="project-no-third-party-components",
        scope_review_ref="review:compliance-scope:empty-inventory:1",
    )

    assert reviewed.allowed is True
    ProductComplianceGate().require_release_allowed(reviewed)
