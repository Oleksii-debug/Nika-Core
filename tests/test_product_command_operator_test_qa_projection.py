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


def _summary() -> ProductProjectSummary:
    return ProductProjectSummary(
        project_id="nika-core",
        version=3,
        title="Nika Core",
        goal="Ship Development Factory MVP",
        state="active",
        updated_at=datetime(2026, 9, 8, tzinfo=UTC),
    )


def test_operator_projection_keeps_test_and_qa_independent() -> None:
    detail = ProductProjectDetail(
        summary=_summary(),
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
        summary=_summary(),
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


def test_operator_projection_wrong_kind_release_state_does_not_clear_build() -> None:
    detail = ProductProjectDetail(
        summary=_summary(),
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
                state="released",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.QA,
                item_id="work-654:qa",
                label="Independent QA",
                state="passed",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.next == "test:work-654:test=released"


def test_operator_projection_wrong_kind_deployment_state_does_not_clear_qa() -> None:
    detail = ProductProjectDetail(
        summary=_summary(),
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
                state="deployed",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.next == "qa:work-654:qa=deployed"


def test_operator_projection_wrong_kind_pass_does_not_clear_deployment() -> None:
    detail = ProductProjectDetail(
        summary=_summary(),
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
                state="passed",
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.DEPLOYMENT,
                item_id="work-654:deployment",
                label="Deployment",
                state="passed",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.next == "integration:work-654:deployment=passed"


def test_operator_projection_retains_candidate_for_canonical_deployment_success_states() -> None:
    candidate_sha = "a" * 40

    for state in ("succeeded", "healthy"):
        detail = ProductProjectDetail(
            summary=_summary(),
            statuses=(
                ProductStatusEntry(
                    kind=ProductStatusKind.COMPONENT,
                    item_id="work-654",
                    label="Issue 654",
                    state="completed",
                ),
                ProductStatusEntry(
                    kind=ProductStatusKind.DEPLOYMENT,
                    item_id="work-654:deployment",
                    label="Deployment",
                    state=state,
                    evidence=(
                        EvidenceReference(
                            kind="git_commit",
                            reference=candidate_sha,
                            label="Candidate",
                        ),
                    ),
                ),
            ),
        )

        projection = project_operator_status(detail)

        assert projection.candidate == candidate_sha
        assert projection.next == "next_work"
