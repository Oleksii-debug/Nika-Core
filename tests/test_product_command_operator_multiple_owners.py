from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.operator_projection import project_operator_status


def test_operator_projection_fails_closed_on_multiple_distinct_owners() -> None:
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
                owner="dev01",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.QA,
                item_id="work-1:qa",
                label="QA",
                state="running",
                owner="dev09",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.owner == "ambiguous_multiple_owners"
    assert "dev01" not in projection.owner
    assert "dev09" not in projection.owner
