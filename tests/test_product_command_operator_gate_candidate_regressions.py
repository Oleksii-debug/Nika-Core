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
            version=3,
            title="Nika Core",
            goal="Ship Development Factory MVP",
            state="active",
            updated_at=datetime(2026, 9, 9, tzinfo=UTC),
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


def test_missing_test_gate_blocks_new_integration_after_terminal_qa() -> None:
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-654", label="Issue 654", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-654:qa", label="Independent QA", state="passed"),
    )
    projection = project_operator_status(detail)
    assert projection.test == "unknown"
    assert projection.qa == "work-654:qa=passed"
    assert projection.integration == "not_started"
    assert projection.next == "test:not_started"


def test_missing_test_gate_blocks_terminal_integration_success() -> None:
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-654", label="Issue 654", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-654:qa", label="Independent QA", state="passed"),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="work-654:integration", label="Integration", state="succeeded"),
    )
    assert project_operator_status(detail).next == "test:not_started"


def test_candidate_survives_pending_integration_without_sha_duplication() -> None:
    historical_sha = "a" * 40
    current_sha = "b" * 40
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-old:qa", label="Historical QA", state="passed", evidence=_candidate(historical_sha)),
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-654", label="Issue 654", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-654:test", label="Automated tests", state="passed", evidence=_candidate(current_sha)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-654:qa", label="Independent QA", state="passed", evidence=_candidate(current_sha)),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="work-654:integration", label="Integration", state="pending"),
    )
    projection = project_operator_status(detail)
    assert projection.candidate == current_sha
    assert historical_sha not in projection.candidate
    assert projection.next == "integration:work-654:integration=pending"


def test_multiple_current_candidate_shas_fail_closed_before_integration() -> None:
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-654", label="Issue 654", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-654:test", label="Automated tests", state="passed", evidence=_candidate("a" * 40)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-654:qa", label="Independent QA", state="passed", evidence=_candidate("b" * 40)),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="work-654:integration", label="Integration", state="pending"),
    )
    projection = project_operator_status(detail)
    assert projection.candidate == "ambiguous_multiple_candidates"
    assert projection.next == "inspect_project:ambiguous_candidate_identity"


def test_failed_qa_never_projects_downstream_success() -> None:
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-654", label="Issue 654", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-654:test", label="Automated tests", state="passed"),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-654:qa", label="Independent QA", state="failed"),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="work-654:integration", label="Integration", state="succeeded"),
    )
    assert project_operator_status(detail).next == "qa:work-654:qa=failed"


def test_terminal_integration_preserves_exact_current_candidate() -> None:
    current_sha = "c" * 40
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-654", label="Issue 654", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-654:test", label="Automated tests", state="passed", evidence=_candidate(current_sha)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-654:qa", label="Independent QA", state="passed", evidence=_candidate(current_sha)),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="work-654:integration", label="Integration", state="succeeded"),
    )
    projection = project_operator_status(detail)
    assert projection.candidate == current_sha
    assert projection.next == "next_work"


def test_historical_and_current_candidate_epochs_fail_closed_without_canonical_epoch_identity() -> None:
    historical_sha = "d" * 40
    current_sha = "e" * 40
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-old", label="Old work", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="work-old:integration", label="Old integration", state="pending", evidence=_candidate(historical_sha)),
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-current", label="Current work", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-current:test", label="Current tests", state="passed", evidence=_candidate(current_sha)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-current:qa", label="Current QA", state="passed", evidence=_candidate(current_sha)),
    )
    projection = project_operator_status(detail)
    assert projection.work == "work-old=completed, work-current=completed"
    assert projection.candidate == "ambiguous_multiple_candidates"
    assert projection.next == "inspect_project:ambiguous_candidate_identity"


def test_previous_work_gates_cannot_satisfy_new_component_without_current_gates() -> None:
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-a", label="Work A", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-a:test", label="Work A tests", state="passed"),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-a:qa", label="Work A QA", state="passed"),
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-b", label="Work B", state="completed"),
    )
    projection = project_operator_status(detail)
    assert projection.work == "work-a=completed, work-b=completed"
    assert projection.test == "work-a:test=passed"
    assert projection.qa == "work-a:qa=passed"
    assert projection.integration == "not_started"
    assert projection.next == "test:not_started:work-b"


def test_two_components_require_component_scoped_qa_before_integration() -> None:
    sha = "f" * 40
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-a", label="Work A", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-b", label="Work B", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-a:test", label="A tests", state="passed", evidence=_candidate(sha)),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-b:test", label="B tests", state="passed", evidence=_candidate(sha)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-a:qa", label="A QA", state="passed", evidence=_candidate(sha)),
    )
    projection = project_operator_status(detail)
    assert projection.candidate == sha
    assert projection.next == "qa:not_started:work-b"


def test_two_components_reject_unscoped_integration_identity() -> None:
    sha = "f" * 40
    detail = _detail(
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-a", label="Work A", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.COMPONENT, item_id="work-b", label="Work B", state="completed"),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-a:test", label="A tests", state="passed", evidence=_candidate(sha)),
        ProductStatusEntry(kind=ProductStatusKind.BUILD, item_id="work-b:test", label="B tests", state="passed", evidence=_candidate(sha)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-a:qa", label="A QA", state="passed", evidence=_candidate(sha)),
        ProductStatusEntry(kind=ProductStatusKind.QA, item_id="work-b:qa", label="B QA", state="passed", evidence=_candidate(sha)),
        ProductStatusEntry(kind=ProductStatusKind.DEPLOYMENT, item_id="integration-shared", label="Shared integration", state="pending"),
    )
    projection = project_operator_status(detail)
    assert projection.next == "inspect_project:ambiguous_integration_identity"
