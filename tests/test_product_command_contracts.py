from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nika_core.product_command.contracts import (
    CommandRouteDecision,
    CommandRouteKind,
    EvidenceReference,
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
    ProductUserDecision,
)
from nika_core.product_command.routing import route_command


def test_product_project_presentation_is_framework_neutral_and_textual() -> None:
    evidence = EvidenceReference(kind="ci", reference="run:123", label="Core CI #123")
    decision = ProductUserDecision(
        decision_id="decision-1",
        title="Deployment target",
        question="Promote this candidate to staging?",
        risk_level=3,
        state="pending",
        evidence=(evidence,),
    )
    summary = ProductProjectSummary(
        project_id="project-1",
        version=2,
        title="Accessible expense app",
        goal="Build and maintain an accessible Windows expense application.",
        state="implementation",
        updated_at=datetime.now(UTC),
        current_decision=decision,
        blocker_count=1,
    )
    detail = ProductProjectDetail(
        summary=summary,
        statuses=(
            ProductStatusEntry(
                kind=ProductStatusKind.BUILD,
                item_id="build-1",
                label="Windows package",
                state="blocked",
                detail="Waiting for exact-SHA CI.",
                evidence=(evidence,),
            ),
        ),
        decisions=(decision,),
        logs=("Build candidate prepared.",),
        errors=("Exact-SHA CI is not green.",),
    )
    payload = detail.model_dump(mode="json")
    assert payload["summary"]["current_decision"]["risk_level"] == 3
    assert payload["statuses"][0]["kind"] == "build"
    assert "pywebview" not in repr(payload).lower()


def test_evidence_checksum_is_exact_sha256() -> None:
    with pytest.raises(ValidationError):
        EvidenceReference(kind="build", reference="artifact:1", label="Build", sha256="abc")


def test_product_command_routes_to_product_project() -> None:
    decision = route_command("Create an accessible Windows application, research it, test and deploy it")
    assert decision.route == CommandRouteKind.PRODUCT_PROJECT
    assert decision.requires_user_decision is False


def test_capability_request_routes_to_toolsmith() -> None:
    decision = route_command("We need a new reusable tool capability for this task")
    assert decision.route == CommandRouteKind.TOOLSMITH


def test_ordinary_request_stays_agent_task() -> None:
    decision = route_command("Summarize the current task log")
    assert decision.route == CommandRouteKind.AGENT_TASK


def test_mixed_product_and_toolsmith_intent_is_not_silently_guessed() -> None:
    decision = route_command("Build a product application and add a missing tool capability")
    assert decision.route == CommandRouteKind.AMBIGUOUS
    assert decision.requires_user_decision is True


def test_ambiguous_contract_cannot_hide_required_user_decision() -> None:
    with pytest.raises(ValidationError):
        CommandRouteDecision(route=CommandRouteKind.AMBIGUOUS, reason="mixed")


def test_command_bounds_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        route_command("   ")
    with pytest.raises(ValueError, match="exceeds"):
        route_command("x" * 4001)
