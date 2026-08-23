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
from nika_core.product_project_integrity import ProductProjectIntegrityService


def _base_spec(count: int = 1) -> ProductProjectSpec:
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
        goal="Build a generic large product",
        desired_outcome="Durable evidence-backed delivery",
        requirements=requirements,
        milestones=milestones,
    )


def _repos(tmp_path, count: int = 1):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="p1",
        name="Generic Product",
        spec=_base_spec(count),
        idempotency_key="create:p1",
    )
    return store, projects, project


def _handoff(projects: ProductProjectRepository) -> None:
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            package_id="research-1",
            evidence=(
                EvidenceRef(
                    evidence_id="evidence-1",
                    provenance_ref="research://report/claim/1",
                    claim="Measured need",
                ),
            ),
        ),
        (
            ProductOption(
                option_id="option-1",
                title="Evidence-backed direction",
                summary="Selected direction",
                evidence_package_ids=("research-1",),
            ),
        ),
    )


def _approve(
    projects: ProductProjectRepository,
    decisions: ProductDecisionRepository,
    *,
    row_version: int,
) -> None:
    _handoff(projects)
    decisions.record(
        "p1",
        ProductDecision(
            decision_id="decision-1",
            option_id="option-1",
            state=ProductDecisionState.APPROVED,
            rationale="Evidence supports this direction",
            decided_by_ref="user://owner",
        ),
        expected_row_version=row_version,
        idempotency_key="decision:1",
    )


def test_integrity_report_survives_restart_and_checks_versions(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    decisions = ProductDecisionRepository(store)
    _approve(projects, decisions, row_version=project.row_version)
    linked = decisions.link_requirement(
        "p1",
        requirement_id="req-0",
        decision_id="decision-1",
        expected_row_version=1,
    )
    linked = projects.update_spec(
        "p1",
        replace(
            linked.spec,
            architecture_decisions=(
                ProductArchitectureDecision(
                    architecture_decision_id="adr-1",
                    title="Architecture",
                    rationale="Research-backed",
                    evidence_package_ids=("research-1",),
                ),
            ),
        ),
        expected_row_version=linked.row_version,
        change_reason="record architecture decision",
    )

    report = ProductProjectIntegrityService(store).validate(
        "p1",
        expected_spec_version=linked.spec_version,
        expected_row_version=linked.row_version,
    )
    assert report.approved_decision_ids == ("decision-1",)
    assert report.research_package_count == 1
    assert report.requirement_count == 1
    assert report.architecture_decision_count == 1

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    assert ProductProjectIntegrityService(restarted).validate("p1") == report
    with pytest.raises(StaleProjectVersionError):
        ProductProjectIntegrityService(restarted).validate(
            "p1",
            expected_row_version=linked.row_version - 1,
        )


def test_integrity_rejects_requirement_reference_to_missing_research(tmp_path) -> None:
    store, _, _ = _repos(tmp_path)
    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT spec_json FROM product_project_specs "
                "WHERE project_id='p1' AND spec_version=1"
            ).fetchone()[0]
        )
        raw["requirements"][0]["evidence_package_ids"] = ["missing-package"]
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='p1' AND spec_version=1",
            (json.dumps(raw),),
        )
    with pytest.raises(ProductProjectError, match="missing research packages"):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_rejects_nonapproved_requirement_decision(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    _handoff(projects)
    decisions = ProductDecisionRepository(store)
    decisions.record(
        "p1",
        ProductDecision(
            decision_id="decision-1",
            option_id="option-1",
            state=ProductDecisionState.PROPOSED,
            rationale="Awaiting approval",
            decided_by_ref="policy://product",
        ),
        expected_row_version=project.row_version,
        idempotency_key="decision:proposed",
    )
    current = projects.get("p1")
    requirement = replace(
        current.spec.requirements[0],
        decision_ids=("decision-1",),
    )
    projects.update_spec(
        "p1",
        replace(current.spec, requirements=(requirement,)),
        expected_row_version=current.row_version,
        change_reason="corrupt test reference",
    )
    with pytest.raises(ProductProjectError, match="non-approved"):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_rejects_decision_evidence_drift(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    decisions = ProductDecisionRepository(store)
    _approve(projects, decisions, row_version=project.row_version)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET evidence_package_ids_json='[]' "
            "WHERE project_id='p1' AND decision_id='decision-1'"
        )
    with pytest.raises(ProductProjectError, match="evidence drifted"):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_rejects_ambiguous_option_after_durable_corruption(tmp_path) -> None:
    store, projects, _ = _repos(tmp_path)
    _handoff(projects)
    payload = {
        "package_id": "research-2",
        "research_artifact_ref": None,
        "evidence": [
            {
                "evidence_id": "evidence-2",
                "provenance_ref": "research://report/claim/2",
                "claim": "Second claim",
            }
        ],
        "options": [
            {
                "option_id": "option-1",
                "title": "Ambiguous duplicate",
                "summary": "Corrupt duplicate identity",
                "evidence_package_ids": ["research-2"],
            }
        ],
    }
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO product_research_handoffs"
            "(project_id,package_id,payload_json,created_at) VALUES (?,?,?,?)",
            ("p1", "research-2", json.dumps(payload), "2026-08-21T00:00:00+00:00"),
        )
    with pytest.raises(ProductProjectError, match="ambiguous product option"):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_rejects_noncontiguous_or_wrong_spec_lineage(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    updated = projects.update_spec(
        "p1",
        project.spec,
        expected_row_version=project.row_version,
        change_reason="revision two",
    )
    with store.connection() as conn:
        raw = json.loads(
            conn.execute(
                "SELECT spec_json FROM product_project_specs "
                "WHERE project_id='p1' AND spec_version=2"
            ).fetchone()[0]
        )
        raw["supersedes_spec_version"] = 99
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='p1' AND spec_version=2",
            (json.dumps(raw),),
        )
    with pytest.raises(ProductProjectError, match="supersedes 99"):
        ProductProjectIntegrityService(store).validate(
            "p1",
            expected_spec_version=updated.spec_version,
        )


def test_large_project_integrity_is_deterministic_across_revisions(tmp_path) -> None:
    store, projects, project = _repos(tmp_path, count=300)
    current = project
    for revision in range(12):
        current = projects.update_spec(
            "p1",
            replace(
                current.spec,
                hypothesis=f"scope revision {revision}",
            ),
            expected_row_version=current.row_version,
            change_reason=f"scope revision {revision}",
        )
        restarted = SQLiteStore(store.path)
        restarted.initialize()
        report = ProductProjectIntegrityService(restarted).validate(
            "p1",
            expected_spec_version=current.spec_version,
            expected_row_version=current.row_version,
        )
        assert report.requirement_count == 300
        assert report.acceptance_criterion_count == 300
        assert report.milestone_count == 300
        assert report.spec_revision_count == current.spec_version
        store = restarted
        projects = ProductProjectRepository(store)


def test_integrity_rejects_missing_architecture_research_package(tmp_path) -> None:
    store, projects, project = _repos(tmp_path)
    current = projects.update_spec(
        "p1",
        replace(
            project.spec,
            architecture_decisions=(
                ProductArchitectureDecision(
                    architecture_decision_id="adr-1",
                    title="Architecture",
                    rationale="Missing provenance",
                    evidence_package_ids=("missing-package",),
                ),
            ),
        ),
        expected_row_version=project.row_version,
        change_reason="architecture proposal",
    )
    assert current.spec_version == 2
    with pytest.raises(ProductProjectError, match="architecture decision"):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_missing_project_remains_not_found(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    with pytest.raises(KeyError, match="missing-project"):
        ProductProjectIntegrityService(store).validate("missing-project")


def test_integrity_fails_closed_when_current_spec_row_is_missing_after_restart(tmp_path) -> None:
    store, _, _ = _repos(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM product_project_specs "
            "WHERE project_id='p1' AND spec_version=1"
        )

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    with pytest.raises(
        ProductProjectError,
        match=r"current ProductProject specification is missing: project_id=p1, spec_version=1",
    ):
        ProductProjectIntegrityService(restarted).validate("p1")


def test_integrity_fails_closed_on_dangling_current_spec_version(tmp_path) -> None:
    store, _, _ = _repos(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET current_spec_version=2 WHERE project_id='p1'"
        )

    with pytest.raises(
        ProductProjectError,
        match=r"current ProductProject specification is missing: project_id=p1, spec_version=2",
    ):
        ProductProjectIntegrityService(store).validate("p1")
