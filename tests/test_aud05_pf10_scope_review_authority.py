from __future__ import annotations

from nika_core.product_compliance import ProductComplianceGate


def test_caller_controlled_scope_review_ref_cannot_mint_release_authority() -> None:
    decision = ProductComplianceGate().evaluate(
        project_id="project-aud05",
        scope_review_ref="candidate:self-declared-review",
    )

    # A non-empty opaque string is not independent proof that the compliance
    # inventory was actually reviewed by an authorized policy/review process.
    assert decision.allowed is False
    assert "compliance-scope:unreviewed" in decision.findings
