from nika_core.product_command.contracts import CommandRouteKind, ProductStatusKind
from nika_core.product_command.coordinator_adapter import coordinator_status_entries
from nika_core.product_command.routing import route_command
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorSnapshot,
    ReviewDecision,
    WorkRecord,
    WorkState,
    WorkerResultEnvelope,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence


def _request(component_id: str = "desktop") -> ComponentWorkRequest:
    return ComponentWorkRequest(
        work_id=f"work-{component_id}",
        project_id="project-1",
        component_id=component_id,
        repository_id="nika-core",
        goal="Implement accessible product component",
        base_sha="a" * 40,
        allowed_paths=(f"src/{component_id}",),
        permission_ceiling=frozenset({"repo.read", "repo.write"}),
        acceptance_commands=(("python", "-m", "pytest"),),
    )


def test_routes_ukrainian_product_command_to_product_project() -> None:
    decision = route_command(
        "Створи застосунок для керування особистими витратами",
        active_project_id="project-1",
    )
    assert decision.route is CommandRouteKind.PRODUCT_PROJECT
    assert decision.project_id == "project-1"
    assert decision.requires_user_decision is False


def test_routes_ukrainian_toolsmith_command_to_toolsmith() -> None:
    decision = route_command("Додай інструмент для перевірки PDF")
    assert decision.route is CommandRouteKind.TOOLSMITH


def test_mixed_ukrainian_product_and_capability_intent_fails_to_ambiguity() -> None:
    decision = route_command(
        "Створи застосунок для бухгалтерії, але спочатку додай інструмент "
        "для імпорту банківських CSV"
    )
    assert decision.route is CommandRouteKind.AMBIGUOUS
    assert decision.requires_user_decision is True
    assert decision.project_id is None


def test_ordinary_ukrainian_task_stays_agent_task() -> None:
    decision = route_command("Поясни останню помилку в журналі")
    assert decision.route is CommandRouteKind.AGENT_TASK


def test_pf2_ready_component_projects_to_textual_component_status() -> None:
    snapshot = CoordinatorSnapshot(
        project_id="project-1",
        revision=3,
        records=(WorkRecord(_request(), WorkState.READY),),
    )

    entries = coordinator_status_entries(snapshot)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind is ProductStatusKind.COMPONENT
    assert entry.item_id == "desktop"
    assert entry.state == "ready"
    assert "Готово до виконання" in entry.detail
    assert "Repository: nika-core" in entry.detail
    assert "Base SHA:" in entry.detail
    assert "Allowed paths:" in entry.detail


def test_pf2_result_review_and_qa_evidence_remain_textually_inspectable() -> None:
    request = _request()
    coding_result = CodingResult(
        job_id=request.work_id,
        test_evidence=(
            TestEvidence(
                command=("python", "-m", "pytest"),
                exit_code=0,
                output_digest="tests-ok",
            ),
        ),
    )
    result = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha="b" * 40,
        diff_digest="c" * 64,
        coding_result=coding_result,
    )
    review = ReviewDecision(
        reviewer_id="qa-agent",
        accepted=True,
        reason="All acceptance evidence passed.",
        evidence_refs=("review://project-1/desktop/1",),
    )
    snapshot = CoordinatorSnapshot(
        project_id="project-1",
        revision=7,
        records=(WorkRecord(request, WorkState.ACCEPTED, result=result, review=review),),
    )

    entries = coordinator_status_entries(snapshot)

    component = next(item for item in entries if item.kind is ProductStatusKind.COMPONENT)
    qa = next(item for item in entries if item.kind is ProductStatusKind.QA)
    assert component.state == "accepted"
    assert "Independent review: accepted by qa-agent" in component.detail
    assert {item.kind for item in component.evidence} == {"git_commit", "diff_digest", "review"}
    assert qa.state == "passed"
    assert "1 command(s)" in qa.detail


def test_pf2_blocker_projects_to_explicit_blocker_entry() -> None:
    request = _request("api")
    snapshot = CoordinatorSnapshot(
        project_id="project-1",
        revision=4,
        records=(WorkRecord(request, WorkState.BLOCKED, blocker="Dependency API unavailable."),),
    )

    entries = coordinator_status_entries(snapshot)

    blocker = next(item for item in entries if item.kind is ProductStatusKind.BLOCKER)
    assert blocker.state == "active"
    assert blocker.detail == "Dependency API unavailable."
    assert blocker.label
