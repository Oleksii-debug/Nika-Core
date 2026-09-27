from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.operator_projection import project_operator_status


def test_operator_projection_normalizes_blank_owner_to_unassigned() -> None:
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
                label="Work",
                state="running",
                owner="   ",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.owner == "unassigned"
