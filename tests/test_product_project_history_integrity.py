from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    EvidenceRef,
    ProductAcceptanceCriterion,
    ProductArchitectureDecision,
    ProductDecision,
    ProductDecisionState,
    ProductMilestone,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)
from nika_core.product_project_history_integrity import (
    ProductProjectHistoricalIntegrityService,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _spec(count: int = 2) -> ProductProjectSpec:
    requirements = tuple(
        ProductRequirement(
            requirement_id=f"req-{index}",
            text=f"Requirement {index}",
            acceptance=(f"Acceptance {index}",),
            acceptance_criteria=(
                ProductAcceptanceCriterion(
                    criterion_id=f"ac-{index}",
                    text=f"Criterion {index}",
                ),
            ),
        )
        for index in range(count)
    )
    milestones = tuple(
        ProductMilestone(
            milestone_id=f"m-{index}",
            title=f"Milestone {index}",
            depends_on_ids=() if index == 0 else (f"m-{index - 1}",),
            acceptance_criterion_ids=(f"ac-{index}",),
        )
        for index in range(count)
    )
    return ProductProjectSpec(
        goal="Build a generic durable product",
        desired_outcome="Evidence-backed accepted delivery",
        requirements=requirements,
        milestones=milestones,
    )


def _repos(tmp_path, *, count: int = 2):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="p1",
        name="Generic Product",
        spec=_spec(count),
        idempotency_key="create:p1",
    )
    return store, projects, project


def _handoff(
    projects: ProductProjectRepository,
    *,
    package_id: str,
    option_id: str,
) -> None:
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            package_id=package_id,
            evidence=(
                EvidenceRef(
                    evidence_id=f"evidence:{package_id}",
                    provenance_ref=f"research://{package_id}/claim/1",
                    claim="Measured evidence",
                ),
            ),
        ),
        (
            ProductOption(
                option_id=option_id,
                title=f"Option {option_id}",
                summary="Evidence-backed direction",
                evidence_package_ids=(package_id,),
            ),
        ),
    )


def _record_decision(
    decisions: ProductDecisionRepository,
    *,
    decision_id: str,
    option_id: str,
    state: ProductDecisionState,
    row_version: int,
) -> None:
    decisions.record(
        "p1",
        ProductDecision(
            decision_id=decision_id,
            option_id=option_id,
            state=state,
            rationale=f"Decision {decision_id}",
            decided_by_ref="policy://product-owner",
        ),
        expected_row_version=row_version,
        idempotency_key=f"decision:{decision_id}:{state.value}",
    )


def test_historical_integrity_reconciles_mixed_pf1_history_across_restart(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    decisions = ProductDecisionRepository(store)
    lifecycle = ProductProjectLifecycleService(store)
    _handoff(projects, package_id="research-1", option_id="option-1")
    _record_decision(
        decisions,
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        row_version=project.row_version,
    )
    linked = decisions.link_requirement(
        "p1",
        requirement_id="req-0",
        decision_id="decision-1",
        expected_row_version=1,
    )
    architecture = projects.update_spec(
        "p1",
        replace(
            linked.spec,
            architecture_decisions=(
                ProductArchitectureDecision(
                    architecture_decision_id="adr-1",
                    title="Evidence-backed architecture",
                    rationale="Selected from research",
                    evidence_package_ids=("research-1",),
                ),
            ),
        ),
        expected_row_version=linked.row_version,
        change_reason="record architecture decision",
    )
    paused = lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=architecture.row_version,
        idempotency_key="status:pause",
        reason="Cross-session review",
        changed_by_ref="user://owner",
    )
    lifecycle.transition(
        "p1",
        ProductProjectState.ACTIVE,
        expected_row_version=paused.row_version,
        idempotency_key="status:resume",
        reason="Review complete",
        changed_by_ref="user://owner",
    )

    report = ProductProjectHistoricalIntegrityService(store).validate("p1")
    assert report.current.spec_version == 3
    assert report.current.row_version == 5
    assert report.historical_decision_reference_count == 2
    assert report.lifecycle_transition_count == 2
    assert report.causal_mutation_count == 5
    assert report.mutation_idempotency_count == 3

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    assert ProductProjectHistoricalIntegrityService(restarted).validate("p1") == report
    with pytest.raises(StaleProjectVersionError):
        ProductProjectHistoricalIntegrityService(restarted).validate(
            "p1",
            expected_row_version=4,
        )


def test_historical_spec_cannot_reference_research_created_in_its_future(tmp_path) -> None:
    store, projects, _ = _repos(tmp_path)
    _handoff(projects, package_id="research-1", option_id="option-1")
    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT spec_json FROM product_project_specs "
                "WHERE project_id='p1' AND spec_version=1"
            ).fetchone()[0]
        )
        raw["requirements"][0]["evidence_package_ids"] = ["research-1"]
        conn.execute(
            "UPDATE product_project_specs SET spec_json=?,created_at=? "
            "WHERE project_id='p1' AND spec_version=1",
            (json.dumps(raw), "2000-01-01T00:00:00+00:00"),
        )
    with pytest.raises(ProductProjectError, match="future research package"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")


def test_historical_spec_cannot_reference_decision_before_approval(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    _handoff(projects, package_id="research-1", option_id="option-1")
    decisions = ProductDecisionRepository(store)
    _record_decision(
        decisions,
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        row_version=project.row_version,
    )
    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT spec_json FROM product_project_specs "
                "WHERE project_id='p1' AND spec_version=1"
            ).fetchone()[0]
        )
        raw["requirements"][0]["decision_ids"] = ["decision-1"]
        conn.execute(
            "UPDATE product_project_specs SET spec_json=?,created_at=? "
            "WHERE project_id='p1' AND spec_version=1",
            (json.dumps(raw), "2000-01-01T00:00:00+00:00"),
        )
    with pytest.raises(ProductProjectError, match="future product decision"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")


def test_causal_integrity_rejects_row_version_without_pf1_mutation(tmp_path) -> None:
    store, _, _ = _repos(tmp_path)
    with store.connection() as conn:
        conn.execute("UPDATE product_projects SET row_version=7 WHERE project_id='p1'")
    with pytest.raises(ProductProjectError, match="no exact PF1 mutation history"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")


def test_causal_integrity_rejects_corrupt_lifecycle_chain_and_status_tail(tmp_path) -> None:
    store, _, _ = _repos(tmp_path)
    lifecycle = ProductProjectLifecycleService(store)
    paused = lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:pause",
        reason="Review",
        changed_by_ref="user://owner",
    )
    lifecycle.transition(
        "p1",
        ProductProjectState.ACTIVE,
        expected_row_version=paused.row_version,
        idempotency_key="status:resume",
        reason="Resume",
        changed_by_ref="user://owner",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT event_id,payload_json FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_id='p1' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["previous_state"] = "blocked"
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload), row["event_id"]),
        )
    with pytest.raises(ProductProjectError, match="lifecycle audit chain"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")


def test_causal_integrity_rejects_missing_decision_audit(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    _handoff(projects, package_id="research-1", option_id="option-1")
    _record_decision(
        ProductDecisionRepository(store),
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.REJECTED,
        row_version=project.row_version,
    )
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE event_type='product_project.decision_recorded' "
            "AND entity_id='p1'"
        )
    with pytest.raises(ProductProjectError, match="decision audit history"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")


def test_causal_integrity_rejects_dangling_known_idempotency_record(tmp_path) -> None:
    store, _, _ = _repos(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO product_project_mutation_idempotency("
            "operation_key,project_id,operation_kind,entity_id,entity_version,"
            "input_fingerprint,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "bogus-decision",
                "p1",
                "product_decision.record",
                "missing-decision",
                1,
                "a" * 64,
                "2026-08-21T00:00:00+00:00",
            ),
        )
    with pytest.raises(ProductProjectError, match="no durable decision"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")


def test_causal_integrity_rejects_missing_spec_or_research_audit(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    _handoff(projects, package_id="research-1", option_id="option-1")
    updated = projects.update_spec(
        "p1",
        replace(project.spec, hypothesis="revision"),
        expected_row_version=project.row_version,
        change_reason="scope revision",
    )
    assert updated.spec_version == 2
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE event_type='product_project.spec_versioned' "
            "AND entity_id='p1'"
        )
    with pytest.raises(ProductProjectError, match="spec revision audit history"):
        ProductProjectHistoricalIntegrityService(store).validate("p1")

    store2, projects2, _ = _repos(tmp_path / "research")
    _handoff(projects2, package_id="research-1", option_id="option-1")
    with store2.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE event_type='product_project.research_handoff' "
            "AND entity_id='p1'"
        )
    with pytest.raises(ProductProjectError, match="research handoff audit history"):
        ProductProjectHistoricalIntegrityService(store2).validate("p1")


def test_long_horizon_mixed_history_survives_many_restart_cycles(tmp_path) -> None:
    store, projects, project = _repos(tmp_path, count=120)
    decisions = ProductDecisionRepository(store)
    lifecycle = ProductProjectLifecycleService(store)

    _handoff(projects, package_id="research-approved", option_id="option-approved")
    _record_decision(
        decisions,
        decision_id="decision-approved",
        option_id="option-approved",
        state=ProductDecisionState.APPROVED,
        row_version=project.row_version,
    )
    current = decisions.link_requirement(
        "p1",
        requirement_id="req-0",
        decision_id="decision-approved",
        expected_row_version=1,
    )

    for cycle in range(12):
        current = projects.update_spec(
            "p1",
            replace(current.spec, hypothesis=f"long-horizon revision {cycle}"),
            expected_row_version=current.row_version,
            change_reason=f"long-horizon revision {cycle}",
        )
        paused = lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=current.row_version,
            idempotency_key=f"status:pause:{cycle}",
            reason=f"checkpoint cycle {cycle}",
            changed_by_ref="policy://long-horizon",
        )
        lifecycle.transition(
            "p1",
            ProductProjectState.ACTIVE,
            expected_row_version=paused.row_version,
            idempotency_key=f"status:resume:{cycle}",
            reason=f"resume cycle {cycle}",
            changed_by_ref="policy://long-horizon",
        )

        package_id = f"research-rejected-{cycle}"
        option_id = f"option-rejected-{cycle}"
        decision_id = f"decision-rejected-{cycle}"
        _handoff(projects, package_id=package_id, option_id=option_id)
        current = projects.get("p1")
        _record_decision(
            ProductDecisionRepository(store),
            decision_id=decision_id,
            option_id=option_id,
            state=ProductDecisionState.REJECTED,
            row_version=current.row_version,
        )

        store = SQLiteStore(store.path)
        store.initialize()
        projects = ProductProjectRepository(store)
        decisions = ProductDecisionRepository(store)
        lifecycle = ProductProjectLifecycleService(store)
        current = projects.get("p1")
        report = ProductProjectHistoricalIntegrityService(store).validate(
            "p1",
            expected_spec_version=current.spec_version,
            expected_row_version=current.row_version,
        )
        assert report.current.requirement_count == 120
        assert report.current.milestone_count == 120
        assert report.lifecycle_transition_count == (cycle + 1) * 2
        assert report.current.spec_revision_count == cycle + 3

    assert current.spec_version == 14
    assert current.row_version == 50
    assert report.causal_mutation_count == 50
    assert report.mutation_idempotency_count == 37
