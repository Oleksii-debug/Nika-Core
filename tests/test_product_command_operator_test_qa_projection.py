from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_command.operator_projection import project_operator_status


def test_operator_projection_keeps_test_and_qa_independent() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 8, tzinfo=UTC),
        ),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-654",
                label="Issue 654",
                state="completed",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.BUILD,
                item_id="work-654:test",
                label="Automated tests",
                state="passed",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.QA,
                item_id="work-654:qa",
                label="Independent QA",
                state="running",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.test == "work-654:test=passed"
    assert projection.qa == "work-654:qa=running"
    assert projection.next == "qa:work-654:qa=running"


def test_operator_projection_does_not_skip_incomplete_test_for_qa() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 8, tzinfo=UTC),
        ),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-654",
                label="Issue 654",
                state="completed",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.BUILD,
                item_id="work-654:test",
                label="Automated tests",
                state="running",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.QA,
                item_id="work-654:qa",
                label="Independent QA",
                state="pending",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.test == "work-654:test=running"
    assert projection.qa == "work-654:qa=pending"
    assert projection.next == "test:work-654:test=running"
