from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import ProductProjectDetail, ProductProjectSummary
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
