from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductProjectSummary,
    ProductUserDecision,
)
from nika_core.product_command.operator_projection import project_operator_status


def test_operator_projection_preserves_goal_before_component_materialization() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=1,
            title="Nika Core",
            goal="develop issue #654 in repository Oleksii-debug/Nika-Core",
            state="active",
            updated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.work == "develop issue #654 in repository Oleksii-debug/Nika-Core"
    assert projection.next == "inspect_project"


def test_operator_projection_preserves_goal_while_owner_decision_is_pending() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=2,
            title="Nika Core",
            goal="develop issue #654 in repository Oleksii-debug/Nika-Core",
            state="blocked",
            updated_at=datetime(2026, 9, 9, tzinfo=UTC),
            current_decision=ProductUserDecision(
                decision_id="choose-approach",
                title="Choose implementation approach",
                question="Which bounded implementation should continue?",
                risk_level=1,
                state="pending",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.work == "develop issue #654 in repository Oleksii-debug/Nika-Core"
    assert projection.next == "owner_decision:choose-approach"
