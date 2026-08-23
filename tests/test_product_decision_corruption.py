from __future__ import annotations

import json

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
    ResearchEvidencePackage,
)


def _environment(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Decision integrity",
        spec=ProductProjectSpec(goal="Decide", desired_outcome="Durable decision"),
        idempotency_key="create:p1",
    )
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            package_id="research-1",
            evidence=(EvidenceRef("ev-1", "research://legacy/1", "Evidence"),),
        ),
        (
            ProductOption(
                option_id="option-1",
                title="Option",
                summary="Evidence-backed option",
                evidence_package_ids=("research-1",),
            ),
        ),
    )
    return store, ProductDecisionRepository(store)


def _decision() -> ProductDecision:
    return ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=ProductDecisionState.APPROVED,
        rationale="Approved",
        decided_by_ref="user://owner",
    )


def _record(decisions: ProductDecisionRepository):
    return decisions.record(
        "p1",
        _decision(),
        expected_row_version=0,
        idempotency_key="decision:approve",
    )


def test_expected_row_version_rejects_bool_instead_of_aliasing_zero(tmp_path) -> None:
    store, decisions = _environment(tmp_path)

    with pytest.raises(ProductProjectError, match="expected ProductProject row_version"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=False,
            idempotency_key="decision:boolean-version",
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0


def test_record_rejects_real_persisted_project_row_version(tmp_path) -> None:
    store, decisions = _environment(tmp_path)
    with store.connection() as conn:
        conn.execute("UPDATE product_projects SET row_version=0.5 WHERE project_id='p1'")

    with pytest.raises(ProductProjectError, match="persisted ProductProject row_version"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:corrupt-project-version",
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0


def test_replay_rejects_real_persisted_entity_version(tmp_path) -> None:
    store, decisions = _environment(tmp_path)
    _record(decisions)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency SET entity_version=1.5 "
            "WHERE operation_key='decision:approve'"
        )

    with pytest.raises(ProductProjectError, match="persisted product decision entity_version"):
        _record(decisions)


def test_get_rejects_real_persisted_decision_version(tmp_path) -> None:
    store, decisions = _environment(tmp_path)
    _record(decisions)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET decision_version=1.5 "
            "WHERE project_id='p1' AND decision_id='decision-1'"
        )

    with pytest.raises(ProductProjectError, match="persisted product decision version"):
        decisions.get("p1", "decision-1")


def test_get_rejects_corrupt_evidence_package_id_json_shape(tmp_path) -> None:
    store, decisions = _environment(tmp_path)
    _record(decisions)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET evidence_package_ids_json=? "
            "WHERE project_id='p1' AND decision_id='decision-1'",
            (json.dumps(["research-1", 7]),),
        )

    with pytest.raises(ProductProjectError, match="must contain non-empty string identifiers"):
        decisions.get("p1", "decision-1")


def test_option_evidence_package_ids_do_not_coerce_non_strings(tmp_path) -> None:
    store, decisions = _environment(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id='p1' AND package_id='research-1'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["options"][0]["evidence_package_ids"] = [7]
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id='p1' AND package_id='research-1'",
            (json.dumps(payload),),
        )

    with pytest.raises(ProductProjectError, match="must be non-empty strings"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:corrupt-package-id",
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_decisions").fetchone()[0] == 0
