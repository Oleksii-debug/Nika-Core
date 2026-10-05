from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
)


def _repositories(
    tmp_path: Path,
) -> tuple[ProductProjectRepository, ProductDecisionRepository]:
    store = SQLiteStore(tmp_path / "Рішення з пробілами" / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="decision-admission",
        name="Продукт із рішенням",
        spec=ProductProjectSpec(
            goal="Перевірити рішення власника",
            desired_outcome="Версійно захищене рішення",
            requirements=(
                ProductRequirement(
                    requirement_id="req-1",
                    text="Власник обирає варіант",
                    acceptance=("Рішення збережене з точною версією",),
                ),
            ),
        ),
        idempotency_key="create-decision-admission",
    )
    package = ResearchEvidencePackage(
        package_id="research-1",
        evidence=(
            EvidenceRef(
                evidence_id="evidence-1",
                provenance_ref="research://decision-admission/source-1",
            ),
        ),
    )
    option = ProductOption(
        option_id="option-1",
        title="Варіант 1",
        summary="Перевірений варіант",
        evidence_package_ids=(package.package_id,),
    )
    projects.record_research_handoff(
        "decision-admission",
        package,
        (option,),
    )
    return projects, ProductDecisionRepository(store)


def _decision(state: ProductDecisionState) -> ProductDecision:
    return ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=state,
        rationale="Потрібне явне рішення власника",
        decided_by_ref="user://owner",
    )


@pytest.mark.parametrize(
    "invalid_version",
    [
        pytest.param(False, id="bool-false"),
        pytest.param(True, id="bool-true"),
        pytest.param(0.0, id="float-zero"),
        pytest.param("0", id="string-zero"),
        pytest.param(-1, id="negative"),
    ],
)
def test_record_decision_rejects_non_exact_row_version_before_mutation(
    tmp_path: Path,
    invalid_version: object,
) -> None:
    projects, decisions = _repositories(tmp_path)

    with pytest.raises(
        ProductProjectError,
        match="expected_row_version must be a non-negative integer",
    ):
        decisions.record(
            "decision-admission",
            _decision(ProductDecisionState.PROPOSED),
            expected_row_version=invalid_version,  # type: ignore[arg-type]
            idempotency_key="record-invalid-version",
        )

    project = projects.get("decision-admission")
    assert project.row_version == 0
    assert project.spec_version == 1
    with pytest.raises(KeyError):
        decisions.get("decision-admission", "decision-1")


def test_record_decision_accepts_exact_zero_row_version(tmp_path: Path) -> None:
    projects, decisions = _repositories(tmp_path)

    stored = decisions.record(
        "decision-admission",
        _decision(ProductDecisionState.PROPOSED),
        expected_row_version=0,
        idempotency_key="record-exact-zero",
    )

    assert stored.decision_version == 1
    assert stored.decision.state is ProductDecisionState.PROPOSED
    assert projects.get("decision-admission").row_version == 1


def test_link_requirement_rejects_float_row_version_without_spec_mutation(
    tmp_path: Path,
) -> None:
    projects, decisions = _repositories(tmp_path)
    decisions.record(
        "decision-admission",
        _decision(ProductDecisionState.PROPOSED),
        expected_row_version=0,
        idempotency_key="record-proposed",
    )
    decisions.record(
        "decision-admission",
        _decision(ProductDecisionState.APPROVED),
        expected_row_version=1,
        idempotency_key="record-approved",
    )
    before = projects.get("decision-admission")
    assert before.row_version == 2
    assert before.spec_version == 1

    with pytest.raises(
        ProductProjectError,
        match="expected_row_version must be a non-negative integer",
    ):
        decisions.link_requirement(
            "decision-admission",
            requirement_id="req-1",
            decision_id="decision-1",
            expected_row_version=2.0,  # type: ignore[arg-type]
        )

    after = projects.get("decision-admission")
    assert after == before
    assert after.spec.requirements[0].decision_ids == ()


def test_link_requirement_accepts_exact_row_version(tmp_path: Path) -> None:
    projects, decisions = _repositories(tmp_path)
    decisions.record(
        "decision-admission",
        _decision(ProductDecisionState.PROPOSED),
        expected_row_version=0,
        idempotency_key="record-proposed-exact",
    )
    decisions.record(
        "decision-admission",
        _decision(ProductDecisionState.APPROVED),
        expected_row_version=1,
        idempotency_key="record-approved-exact",
    )

    updated = decisions.link_requirement(
        "decision-admission",
        requirement_id="req-1",
        decision_id="decision-1",
        expected_row_version=2,
    )

    assert updated.row_version == 3
    assert updated.spec_version == 2
    assert updated.spec.requirements[0].decision_ids == ("decision-1",)
