from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectPresentationConsistencyError,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductAcceptanceCriterion,
    ProductArchitectureDecision,
    ProductBlocker,
    ProductDecision,
    ProductDecisionState,
    ProductMilestone,
    ProductOption,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ProductRequirementKind,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)
from nika_core.product_project_lifecycle import ProductProjectState


def _service(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    return ProductProjectCommandService(projects), projects, store


def _spec(goal: str = "Build accessible expense app") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A tested accessible Windows application",
        requirements=(
            ProductRequirement(
                requirement_id="req-keyboard",
                text="Keyboard and screen-reader operation",
                acceptance=("All primary actions are keyboard reachable",),
                evidence_package_ids=("research-accessibility",),
                kind=ProductRequirementKind.ACCESSIBILITY,
                acceptance_criteria=(
                    ProductAcceptanceCriterion(
                        "criterion-keyboard",
                        "Primary actions have deterministic keyboard paths",
                        "automated_semantic_test_plus_human_nvda",
                    ),
                ),
            ),
        ),
        repository_refs=("repo://expense/core",),
        team_refs=("role://accessibility",),
        credential_refs=("credential://github/project-writer",),
        risk={"level": "R3"},
        milestones=(
            ProductMilestone(
                "milestone-accessibility",
                "Accessibility acceptance",
                acceptance_criterion_ids=("criterion-keyboard",),
            ),
        ),
        blockers=(
            ProductBlocker(
                "blocker-nvda",
                "Human NVDA evidence is not available yet",
                blocking_milestone_ids=("milestone-accessibility",),
                evidence_refs=("evidence://accessibility/pending",),
            ),
        ),
        architecture_decisions=(
            ProductArchitectureDecision(
                "adr-semantic-first",
                "Semantic-first interaction",
                "Prefer native/API and semantic accessibility surfaces before vision.",
                evidence_package_ids=("research-accessibility",),
            ),
        ),
    )


def _create(service: ProductProjectCommandService) -> None:
    service.create_project(
        project_id="p1",
        name="Accessible Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )


def _handoff(
    projects: ProductProjectRepository,
    *,
    package_id: str = "research-accessibility",
    option_id: str = "option-semantic",
) -> None:
    package = ResearchEvidencePackage(
        package_id,
        (
            EvidenceRef(
                f"evidence:{package_id}",
                f"research://{package_id}/claim/1",
                "Evidence-backed product option",
            ),
        ),
    )
    projects.record_research_handoff(
        "p1",
        package,
        (ProductOption(option_id, option_id, "Evidence-backed option", (package_id,)),),
    )


def _decision(
    state: ProductDecisionState,
    *,
    decision_id: str = "decision-ui",
    option_id: str = "option-semantic",
) -> ProductDecision:
    return ProductDecision(
        decision_id=decision_id,
        option_id=option_id,
        state=state,
        rationale="Owner-visible evidence-backed choice",
        decided_by_ref="user://owner",
    )


def test_structured_product_spec_is_visible_without_credential_reference(tmp_path) -> None:
    service, _projects, _store = _service(tmp_path)
    _create(service)

    detail = service.inspect_project("p1")
    serialized = detail.model_dump_json()
    by_kind = {kind: [] for kind in ProductStatusKind}
    for item in detail.statuses:
        by_kind[item.kind].append(item)

    requirement = by_kind[ProductStatusKind.REQUIREMENT][0]
    milestone = by_kind[ProductStatusKind.MILESTONE][0]
    blocker = by_kind[ProductStatusKind.BLOCKER][0]
    architecture = by_kind[ProductStatusKind.ARCHITECTURE_DECISION][0]

    assert "accessibility" in requirement.detail
    assert "criterion-keyboard" in requirement.detail
    assert milestone.state == "blocked"
    assert blocker.item_id == "blocker-nvda"
    assert architecture.item_id == "adr-semantic-first"
    assert detail.summary.blocker_count == 1
    assert "credential://github/project-writer" not in serialized
    assert "research-accessibility" in serialized


def test_real_product_decision_proposed_approved_restart_and_history(tmp_path) -> None:
    service, projects, store = _service(tmp_path)
    _create(service)
    _handoff(projects)

    proposed = service.record_decision(
        "p1",
        _decision(ProductDecisionState.PROPOSED),
        expected_row_version=0,
        idempotency_key="decision:proposed",
    )
    assert proposed.summary.current_decision is not None
    assert proposed.summary.current_decision.state == "pending"
    assert proposed.summary.current_decision.risk_level == 3
    assert proposed.decisions[0].evidence[0].reference == "research-accessibility"

    approved = service.persist_decision(
        "p1",
        _decision(ProductDecisionState.APPROVED),
        expected_row_version=1,
        idempotency_key="decision:approved",
    )
    assert approved.summary.current_decision is None
    assert approved.decisions[0].state == "approved"
    assert [item.state for item in service.decision_history("p1", "decision-ui")] == [
        "pending",
        "approved",
    ]

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectCommandService(ProductProjectRepository(restarted_store))
    restarted_detail = restarted.inspect_project("p1")
    assert restarted_detail.decisions[0].state == "approved"
    assert restarted_detail.decisions[0].evidence[0].reference == "research-accessibility"


def test_multiple_pending_decisions_are_not_silently_auto_selected(tmp_path) -> None:
    service, projects, _store = _service(tmp_path)
    _create(service)
    _handoff(projects, package_id="research-a", option_id="option-a")
    _handoff(projects, package_id="research-b", option_id="option-b")

    service.record_decision(
        "p1",
        _decision(
            ProductDecisionState.PROPOSED,
            decision_id="decision-a",
            option_id="option-a",
        ),
        expected_row_version=0,
        idempotency_key="decision:a",
    )
    detail = service.record_decision(
        "p1",
        _decision(
            ProductDecisionState.PROPOSED,
            decision_id="decision-b",
            option_id="option-b",
        ),
        expected_row_version=1,
        idempotency_key="decision:b",
    )

    assert len(detail.decisions) == 2
    assert detail.summary.current_decision is None
    assert "2 product decisions require owner review" in detail.logs[0]


def test_approved_decision_can_be_explicitly_linked_to_requirement(tmp_path) -> None:
    service, projects, _store = _service(tmp_path)
    _create(service)
    _handoff(projects)
    service.record_decision(
        "p1",
        _decision(ProductDecisionState.APPROVED),
        expected_row_version=0,
        idempotency_key="decision:approved",
    )

    linked = service.link_decision_requirement(
        "p1",
        requirement_id="req-keyboard",
        decision_id="decision-ui",
        expected_row_version=1,
    )

    requirement = next(
        item for item in linked.statuses if item.kind is ProductStatusKind.REQUIREMENT
    )
    assert linked.summary.version == 2
    assert "decision-ui" in requirement.detail


def test_lifecycle_transition_is_durable_and_restart_visible(tmp_path) -> None:
    service, _projects, store = _service(tmp_path)
    _create(service)

    paused = service.transition_project(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="state:paused",
        reason="Owner requested a controlled pause",
        changed_by_ref="user://owner",
    )
    assert paused.summary.state == "paused"
    history = service.lifecycle_history("p1")
    assert history[-1].new_state is ProductProjectState.PAUSED

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectCommandService(ProductProjectRepository(restarted_store))
    assert restarted.inspect_project("p1").summary.state == "paused"
    assert restarted.lifecycle_history("p1")[-1].new_state is ProductProjectState.PAUSED


def test_stale_visible_spec_version_still_fails_closed(tmp_path) -> None:
    service, _projects, _store = _service(tmp_path)
    _create(service)
    service.update_project("p1", expected_spec_version=1, goal="v2")

    with pytest.raises(StaleProjectVersionError, match="stale ProductProject spec"):
        service.update_project("p1", expected_spec_version=1, goal="stale")


def test_full_spec_replacement_cannot_mix_with_partial_update(tmp_path) -> None:
    service, _projects, _store = _service(tmp_path)
    _create(service)

    with pytest.raises(ValueError, match="cannot be combined"):
        service.update_project(
            "p1",
            expected_spec_version=1,
            spec=_spec("replacement"),
            goal="partial",
        )


def test_presentation_race_between_project_and_decision_reads_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    service, projects, _store = _service(tmp_path)
    _create(service)
    original_list = service._decisions.list

    def mutate_during_read(project_id: str):
        decisions = original_list(project_id)
        current = projects.get(project_id)
        projects.update_spec(
            project_id,
            replace(current.spec, goal="mutated during presentation"),
            expected_row_version=current.row_version,
        )
        return decisions

    monkeypatch.setattr(service._decisions, "list", mutate_during_read)

    with pytest.raises(
        ProductProjectPresentationConsistencyError,
        match="changed while PF5 was composing presentation",
    ):
        service.inspect_project("p1")


def test_long_structured_identifiers_are_bounded_without_raw_credential_leak(tmp_path) -> None:
    service, _projects, _store = _service(tmp_path)
    long_id = "req-" + "x" * 300
    spec = replace(
        _spec(),
        requirements=(
            ProductRequirement(
                requirement_id=long_id,
                text="A" * 500,
                acceptance=("deterministic",),
            ),
        ),
        milestones=(),
        blockers=(),
        architecture_decisions=(),
    )
    detail = service.create_project(
        project_id="p1",
        name="N" * 500,
        spec=spec,
        idempotency_key="create:bounded",
    )
    requirement = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.REQUIREMENT
    )

    assert len(requirement.item_id) <= 160
    assert len(requirement.label) <= 240
    assert requirement.item_id != long_id[:160]
    assert "credential://github/project-writer" not in detail.model_dump_json()
