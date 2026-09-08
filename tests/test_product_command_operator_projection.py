from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
    ProductUserDecision,
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
            updated_at=datetime(2026, 9, 8, tzinfo=UTC),
        ),
        statuses=statuses,
    )


def test_operator_projection_exposes_required_factory_fields() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="review_required",
            owner="dev06",
            evidence=(
                EvidenceReference(
                    kind="git_commit",
                    reference="a" * 40,
                    label="Exact candidate SHA",
                ),
            ),
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="Tests",
            state="passed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.DEPLOYMENT,
            item_id="integration-654",
            label="Integration",
            state="pending",
        ),
    )

    projection = project_operator_status(detail).model_dump(by_alias=True)

    assert tuple(projection) == (
        "PROJECT",
        "WORK",
        "OWNER",
        "STATE",
        "BLOCKER",
        "CANDIDATE",
        "TEST",
        "QA",
        "INTEGRATION",
        "NEXT",
    )
    assert projection == {
        "PROJECT": "nika-core",
        "WORK": "work-654=review_required",
        "OWNER": "dev06",
        "STATE": "active",
        "BLOCKER": "none",
        "CANDIDATE": "a" * 40,
        "TEST": "passed",
        "QA": "work-654:qa=passed",
        "INTEGRATION": "integration-654=pending",
        "NEXT": "continue_work:work-654",
    }


def test_operator_projection_prioritizes_blocker_without_inventing_progress() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="blocked",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.BLOCKER,
            item_id="work-654:blocker",
            label="Blocked",
            state="active",
            detail="Missing credential",
        ),
    )

    projection = project_operator_status(detail).model_dump(by_alias=True)

    assert projection["BLOCKER"] == "work-654:blocker=active"
    assert projection["CANDIDATE"] == "unknown"
    assert projection["TEST"] == "unknown"
    assert projection["QA"] == "unknown"
    assert projection["INTEGRATION"] == "not_started"
    assert projection["NEXT"] == "resolve_blocker"


def test_operator_projection_surfaces_pending_owner_decision_before_next_work() -> None:
    decision = ProductUserDecision(
        decision_id="decision-1",
        title="Approve scope",
        question="Proceed?",
        risk_level=2,
        state="pending",
    )
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 8, tzinfo=UTC),
            current_decision=decision,
        ),
        decisions=(decision,),
    )

    projection = project_operator_status(detail)

    assert projection.next == "owner_decision:decision-1"
    assert projection.work == "none"
    assert projection.owner == "unassigned"
