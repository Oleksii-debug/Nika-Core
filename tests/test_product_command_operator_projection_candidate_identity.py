from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.operator_projection import project_operator_status


def test_operator_projection_fails_closed_on_non_exact_git_commit_reference() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-654",
                label="Issue 654",
                state="review_required",
                evidence=(
                    EvidenceReference(
                        kind="git_commit",
                        reference="not-an-exact-commit-sha",
                        label="Malformed candidate identity",
                    ),
                ),
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == "invalid_candidate_identity"
    assert "not-an-exact-commit-sha" not in projection.candidate
