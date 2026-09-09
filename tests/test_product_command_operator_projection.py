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


def test_operator_projection_terminal_owner_decision_does_not_mask_qa() -> None:
    for state in ("approved", "rejected", "superseded"):
        decision = ProductUserDecision(
            decision_id=f"decision-{state}",
            title="Scope decision",
            question="Proceed?",
            risk_level=2,
            state=state,
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
            statuses=(
                ProductStatusEntry(
                    kind=ProductStatusKind.COMPONENT,
                    item_id="work-654",
                    label="Issue 654",
                    state="completed",
                ),
                ProductStatusEntry(
                    kind=ProductStatusKind.QA,
                    item_id="work-654:qa",
                    label="QA",
                    state="running",
                ),
            ),
            decisions=(decision,),
        )

        projection = project_operator_status(detail)

        assert projection.next == "qa:work-654:qa=running"


def test_operator_projection_fails_closed_on_multiple_candidate_shas() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="review_required",
            evidence=(
                EvidenceReference(
                    kind="git_commit",
                    reference="a" * 40,
                    label="Previous candidate SHA",
                ),
            ),
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="Tests",
            state="running",
            evidence=(
                EvidenceReference(
                    kind="git_commit",
                    reference="b" * 40,
                    label="Current candidate SHA",
                ),
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == "ambiguous_multiple_candidates"
    assert ("a" * 40) not in projection.candidate
    assert ("b" * 40) not in projection.candidate


def test_operator_projection_does_not_advance_past_pending_qa() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="QA",
            state="running",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.next == "qa:work-654:qa=running"


def test_operator_projection_does_not_advance_past_pending_integration() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="QA",
            state="passed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.DEPLOYMENT,
            item_id="integration-654",
            label="Integration",
            state="pending",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.next == "integration:integration-654=pending"


def test_operator_projection_completed_blocker_does_not_mask_downstream_qa() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.BLOCKER,
            item_id="work-654:blocker",
            label="Credential blocker",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="QA",
            state="running",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.blocker == "none"
    assert projection.next == "qa:work-654:qa=running"


def test_operator_projection_resolved_blocker_does_not_mask_downstream_qa() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.BLOCKER,
            item_id="work-654:blocker",
            label="Credential blocker",
            state="resolved",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="QA",
            state="running",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.blocker == "none"
    assert projection.next == "qa:work-654:qa=running"


def test_operator_projection_trims_terminal_state_for_progression() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-654",
            label="Issue 654",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.BLOCKER,
            item_id="work-654:blocker",
            label="Credential blocker",
            state="  ReSoLvEd  ",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-654:qa",
            label="QA",
            state="running",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.blocker == "none"
    assert projection.next == "qa:work-654:qa=running"


def test_operator_projection_fails_closed_on_unrepresented_summary_blockers() -> None:
    detail = ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 8, tzinfo=UTC),
            blocker_count=2,
        ),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-654",
                label="Issue 654",
                state="completed",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.blocker == "unrepresented_blockers=2"
    assert projection.next == "resolve_blocker"
