from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    EvidenceRef,
    ProductAcceptanceCriterion,
    ProductArchitectureDecision,
    ProductBlocker,
    ProductMilestone,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ProductRequirementKind,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)


def _repo(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store, ProductProjectRepository(store)


def _criterion(i: int) -> ProductAcceptanceCriterion:
    return ProductAcceptanceCriterion(f"ac-{i}", f"criterion {i}", "automated_test")


def _requirement(i: int) -> ProductRequirement:
    criterion = _criterion(i)
    return ProductRequirement(
        requirement_id=f"req-{i}",
        text=f"requirement {i}",
        acceptance=(criterion.text,),
        kind=ProductRequirementKind.FUNCTIONAL if i % 2 == 0 else ProductRequirementKind.SECURITY,
        acceptance_criteria=(criterion,),
    )


def _large_spec(size: int = 250, *, revision: int = 0) -> ProductProjectSpec:
    requirements = tuple(_requirement(i) for i in range(size))
    milestones = tuple(
        ProductMilestone(
            milestone_id=f"ms-{i}",
            title=f"milestone {i}",
            depends_on_ids=() if i == 0 else (f"ms-{i - 1}",),
            acceptance_criterion_ids=(f"ac-{i}",),
        )
        for i in range(size)
    )
    return ProductProjectSpec(
        goal=f"large generic product revision {revision}",
        desired_outcome="durable product",
        requirements=requirements,
        milestones=milestones,
        blockers=(
            ProductBlocker(
                "blocker-1",
                "external dependency",
                (f"ms-{size - 1}",),
                ("evidence://1",),
            ),
        ),
        architecture_decisions=(
            ProductArchitectureDecision("adr-1", "Modular", "bounded architecture"),
            ProductArchitectureDecision(
                "adr-2",
                "Revision",
                "supersede initial architecture",
                supersedes_decision_id="adr-1",
            ),
        ),
        repository_refs=tuple(f"repo://component-{i}" for i in range(30)),
        team_refs=tuple(f"team://role-{i}" for i in range(20)),
        credential_refs=("credential://github/project-writer",),
    )


def test_large_spec_and_repeated_revisions_survive_restart(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    project = repo.create(
        project_id="large",
        name="Large Product",
        spec=_large_spec(),
        idempotency_key="create:large",
    )
    for revision in range(1, 13):
        project = repo.update_spec(
            "large",
            _large_spec(revision=revision),
            expected_row_version=project.row_version,
            change_reason=f"scope revision {revision}",
        )
    assert project.spec_version == 13
    assert len(project.spec.requirements) == 250
    assert len(project.spec.milestones) == 250

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    recovered_repo = ProductProjectRepository(restarted)
    recovered = recovered_repo.get("large")
    assert recovered.spec_version == 13
    assert recovered.spec.milestones[-1].depends_on_ids == ("ms-248",)
    history = recovered_repo.spec_history("large")
    assert len(history) == 13
    assert history[0].supersedes_spec_version is None
    assert history[-1].supersedes_spec_version == 12
    assert history[-1].change_reason == "scope revision 12"


def test_stale_revision_fails_closed(tmp_path) -> None:
    _, repo = _repo(tmp_path)
    created = repo.create(project_id="p", name="P", spec=_large_spec(3), idempotency_key="create:p")
    repo.update_spec("p", _large_spec(3, revision=1), expected_row_version=created.row_version)
    with pytest.raises(StaleProjectVersionError):
        repo.update_spec("p", _large_spec(3, revision=2), expected_row_version=created.row_version)


def test_duplicate_requirement_identity_fails_closed() -> None:
    requirement = _requirement(1)
    with pytest.raises(ProductProjectError, match="duplicate product requirement id"):
        ProductProjectSpec(
            goal="g",
            desired_outcome="o",
            requirements=(requirement, replace(requirement, text="duplicate")),
        )


def test_milestone_unknown_dependency_and_cycle_fail_closed() -> None:
    with pytest.raises(ProductProjectError, match="unknown dependencies"):
        ProductProjectSpec(
            goal="g",
            desired_outcome="o",
            milestones=(ProductMilestone("a", "A", ("missing",)),),
        )
    with pytest.raises(ProductProjectError, match="cycle"):
        ProductProjectSpec(
            goal="g",
            desired_outcome="o",
            milestones=(
                ProductMilestone("a", "A", ("b",)),
                ProductMilestone("b", "B", ("a",)),
            ),
        )


def test_raw_token_shaped_values_fail_even_under_innocent_keys() -> None:
    with pytest.raises(ProductProjectError, match="token-shaped"):
        ProductProjectSpec(
            goal="g",
            desired_outcome="o",
            risk={"note": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"},
        )
    with pytest.raises(ProductProjectError, match="token-shaped"):
        ProductProjectSpec(
            goal="g",
            desired_outcome="o",
            credential_refs=("ghp_abcdefghijklmnopqrstuvwxyz1234567890",),
        )


def test_phantom_research_evidence_package_fails_closed(tmp_path) -> None:
    _, repo = _repo(tmp_path)
    repo.create(
        project_id="p",
        name="P",
        spec=ProductProjectSpec(goal="g", desired_outcome="o"),
        idempotency_key="create:p",
    )
    package = ResearchEvidencePackage("research-1", (EvidenceRef("ev-1", "research://1"),))
    with pytest.raises(ProductProjectError, match="unknown evidence package"):
        repo.record_research_handoff(
            "p",
            package,
            (ProductOption("option", "Option", "Summary", ("research-1", "phantom")),),
        )


def test_handoff_can_link_previously_recorded_package(tmp_path) -> None:
    _, repo = _repo(tmp_path)
    repo.create(
        project_id="p",
        name="P",
        spec=ProductProjectSpec(goal="g", desired_outcome="o"),
        idempotency_key="create:p",
    )
    first = ResearchEvidencePackage("research-1", (EvidenceRef("ev-1", "research://1"),))
    repo.record_research_handoff(
        "p",
        first,
        (ProductOption("option-1", "Option 1", "Summary", ("research-1",)),),
    )
    second = ResearchEvidencePackage("research-2", (EvidenceRef("ev-2", "research://2"),))
    repo.record_research_handoff(
        "p",
        second,
        (ProductOption("option-2", "Option 2", "Summary", ("research-2", "research-1")),),
    )


def test_legacy_spec_payload_still_loads(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    spec = ProductProjectSpec(
        goal="legacy",
        desired_outcome="works",
        requirements=(ProductRequirement("req", "text", ("old acceptance",)),),
    )
    created = repo.create(project_id="legacy", name="Legacy", spec=spec, idempotency_key="legacy")
    assert created.spec.requirements[0].kind is ProductRequirementKind.FUNCTIONAL
    legacy_payload = {
        "goal": "legacy",
        "desired_outcome": "works",
        "hypothesis": "",
        "requirements": [
            {
                "requirement_id": "req",
                "text": "text",
                "acceptance": ["old acceptance"],
                "evidence_package_ids": [],
                "decision_ids": [],
            }
        ],
        "architecture_decision_refs": [],
        "repository_refs": [],
        "team_refs": [],
        "artifact_refs": [],
        "build_refs": [],
        "release_refs": [],
        "deployment_refs": [],
        "incident_refs": [],
        "credential_refs": [],
        "budget": {},
        "risk": {},
        "compliance": {},
    }
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='legacy' AND spec_version=1",
            (json.dumps(legacy_payload),),
        )
    recovered = repo.get("legacy")
    assert recovered.spec.requirements[0].acceptance == ("old acceptance",)
    assert recovered.spec.milestones == ()
