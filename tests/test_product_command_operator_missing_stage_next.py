from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.operator_projection import project_operator_status


def _detail(*statuses: ProductStatusEntry) -> ProductProjectDetail:
    return ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
        statuses=statuses,
    )


def test_operator_projection_exposes_missing_test_as_next_gate() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.test == "unknown"
    assert projection.qa == "unknown"
    assert projection.next == "test:not_started"


def test_operator_projection_exposes_missing_qa_after_successful_test() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.BUILD,
            item_id="work-654:test",
            label="Tests",
            state="passed",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.test == "work-654:test=passed"
    assert projection.qa == "unknown"
    assert projection.next == "qa:not_started"
