from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.operator_projection import project_operator_status


def test_operator_projection_requires_candidate_identity_before_integration() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=1,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-1",
                label="Issue 1",
                state="completed",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.BUILD,
                item_id="work-1:test",
                label="Exact candidate tests",
                state="passed",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.QA,
                item_id="work-1:qa",
                label="Independent QA",
                state="passed",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == "unknown"
    assert projection.integration == "not_started"
    assert projection.next == "inspect_project:missing_candidate_identity"
