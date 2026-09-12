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


def _detail(*statuses: ProductStatusEntry) -> ProductProjectDetail:
    return ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id="nika-core",
            version=1,
            title="Nika Core",
            goal="Finish current Factory work",
            state="active",
            updated_at=datetime(2026, 9, 12, tzinfo=UTC),
        ),
        statuses=statuses,
    )


def _candidate(sha: str) -> tuple[EvidenceReference, ...]:
    return (
        EvidenceReference(
            kind="git_commit",
            reference=sha,
            label="Exact candidate SHA",
        ),
    )


def test_unique_active_component_excludes_historical_owner_and_qa() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-old:qa",
            label="Historical QA",
            state="passed",
            owner="dev-old",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-current",
            label="Current work",
            state="ready",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.owner == "unassigned"
    assert projection.test == "unknown"
    assert projection.qa == "unknown"
    assert projection.next == "continue_work:work-current"


def test_unique_active_component_owner_ignores_historical_owner() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-old:qa",
            label="Historical QA",
            state="passed",
            owner="dev-old",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-current",
            label="Current work",
            state="review_required",
            owner="dev-current",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.owner == "dev-current"
    assert projection.qa == "unknown"


def test_project_integration_stays_project_scoped_without_polluting_work_owner() -> None:
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.BUILD,
            item_id="work-old:test",
            label="Historical tests",
            state="passed",
            owner="dev-old",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.QA,
            item_id="work-old:qa",
            label="Historical QA",
            state="passed",
            owner="dev-old",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-current",
            label="Current work",
            state="ready",
            owner="dev-current",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.DEPLOYMENT,
            item_id="deployment-operation:op-1",
            label="Project deployment",
            state="pending",
            owner="node-1",
        ),
    )

    projection = project_operator_status(detail)

    assert projection.owner == "dev-current"
    assert projection.test == "unknown"
    assert projection.qa == "unknown"
    assert projection.integration == "deployment-operation:op-1=pending"
    assert projection.next == "continue_work:work-current"


def test_project_integration_git_commit_cannot_mint_component_candidate() -> None:
    deployment_sha = "a" * 40
    detail = _detail(
        ProductStatusEntry(
            kind=ProductStatusKind.COMPONENT,
            item_id="work-current",
            label="Current work",
            state="completed",
        ),
        ProductStatusEntry(
            kind=ProductStatusKind.DEPLOYMENT,
            item_id="deployment-operation:op-1",
            label="Project deployment",
            state="pending",
            evidence=_candidate(deployment_sha),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == "unknown"
    assert deployment_sha not in projection.candidate
    assert projection.next == "test:not_started"
