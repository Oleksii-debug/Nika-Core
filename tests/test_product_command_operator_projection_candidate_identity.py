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
        updated_at=datetime(2026, 9, 9, tzinfo=UTC),
    )


def test_operator_projection_fails_closed_on_non_exact_git_commit_reference() -> None:
    detail = ProductProjectDetail(
        summary=_summary(),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-654",
                label="Issue 654",
                state="review_required",
                evidence=(
                    EvidenceReference(
                        kind="git_commit",
                        reference="not-an-exact-commit-sha",
                        label="Malformed candidate identity",
                    ),
                ),
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == "invalid_candidate_identity"
    assert "not-an-exact-commit-sha" not in projection.candidate


def test_operator_projection_does_not_present_terminal_historical_commit_as_current() -> None:
    historical_sha = "a" * 40
    detail = ProductProjectDetail(
        summary=_summary(),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-previous",
                label="Previous issue",
                state="completed",
                evidence=(
                    EvidenceReference(
                        kind="git_commit",
                        reference=historical_sha,
                        label="Historical candidate SHA",
                    ),
                ),
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-current",
                label="Current issue",
                state="ready",
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == "unknown"
    assert historical_sha not in projection.candidate


def test_operator_projection_uses_candidate_evidence_only_from_active_status() -> None:
    historical_sha = "a" * 40
    current_sha = "b" * 40
    detail = ProductProjectDetail(
        summary=_summary(),
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-previous",
                label="Previous issue",
                state="completed",
                evidence=(
                    EvidenceReference(
                        kind="git_commit",
                        reference=historical_sha,
                        label="Historical candidate SHA",
                    ),
                ),
            ),
            ProductStatusEntry(
                kind=ProductStatusKind.COMPONENT,
                item_id="work-current",
                label="Current issue",
                state="review_required",
                evidence=(
                    EvidenceReference(
                        kind="git_commit",
                        reference=current_sha,
                        label="Current candidate SHA",
                    ),
                ),
            ),
        ),
    )

    projection = project_operator_status(detail)

    assert projection.candidate == current_sha
    assert historical_sha not in projection.candidate
