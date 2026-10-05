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


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Test project",
        spec=ProductProjectSpec(goal="Build a product", desired_outcome="Verified product"),
        idempotency_key="create:p1",
    )
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            "research-1",
            (EvidenceRef("evidence-1", "research://source-1", "Relevant evidence"),),
        ),
        (ProductOption("option-1", "Option", "Summary", ("research-1",)),),
    )
    return store, projects, ProductDecisionRepository(store)


def _approve(decisions):
    return decisions.record(
        "p1",
        ProductDecision(
            "decision-1",
            "option-1",
            ProductDecisionState.APPROVED,
            "Evidence supports the option",
            "user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:approve:1",
    )


def _corrupt(payload, case):
    if case == "json":
        return "{"
    if case == "array":
        return "[]"
    if case == "scalar":
        return "null"
    if case == "blob":
        return b"\xff"
    if case == "number":
        return 42
    if case == "wrong-package":
        payload["package_id"] = "other"
    elif case == "missing-options":
        del payload["options"]
    elif case == "non-list-options":
        payload["options"] = "option-1"
    elif case == "invalid-other-option":
        payload["options"].append(False)
    elif case == "duplicate-options":
        payload["options"].append(dict(payload["options"][0]))
    elif case == "empty-evidence":
        payload["evidence"] = []
    elif case == "wrong-evidence-type":
        payload["evidence"] = "evidence-1"
    elif case == "invalid-evidence-entry":
        payload["evidence"][0] = None
    elif case == "empty-provenance":
        payload["evidence"][0]["provenance_ref"] = ""
    elif case == "duplicate-evidence":
        payload["evidence"].append(dict(payload["evidence"][0]))
    elif case == "invalid-option-id":
        payload["options"][0]["option_id"] = False
    elif case == "missing-package-ids":
        del payload["options"][0]["evidence_package_ids"]
    elif case == "string-package-ids":
        payload["options"][0]["evidence_package_ids"] = "research-1"
    elif case == "empty-package-ids":
        payload["options"][0]["evidence_package_ids"] = []
    elif case == "duplicate-package-ids":
        payload["options"][0]["evidence_package_ids"].append("research-1")
    elif case == "invalid-package-id":
        payload["options"][0]["evidence_package_ids"] = [False]
    elif case == "foreign-package":
        payload["options"][0]["evidence_package_ids"] = ["research-2"]
    elif case == "nonfinite":
        payload["untrusted"] = float("nan")
    elif case == "duplicate-key":
        raw = json.dumps(payload, ensure_ascii=False)
        return raw.replace('"package_id":', '"package_id": "shadow", "package_id":', 1)
    else:
        raise AssertionError(case)
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "case",
    (
        "json",
        "array",
        "scalar",
        "blob",
        "number",
        "wrong-package",
        "missing-options",
        "non-list-options",
        "invalid-other-option",
        "duplicate-options",
        "empty-evidence",
        "wrong-evidence-type",
        "invalid-evidence-entry",
        "empty-provenance",
        "duplicate-evidence",
        "invalid-option-id",
        "missing-package-ids",
        "string-package-ids",
        "empty-package-ids",
        "duplicate-package-ids",
        "invalid-package-id",
        "foreign-package",
        "nonfinite",
        "duplicate-key",
    ),
)
def test_corrupt_research_cannot_authorize_product_decision(tmp_path, case):
    store, projects, decisions = _setup(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id=? AND package_id=?",
            ("p1", "research-1"),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id=? AND package_id=?",
            (_corrupt(payload, case), "p1", "research-1"),
        )

    with pytest.raises(ProductProjectError, match="malformed"):
        _approve(decisions)

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions WHERE project_id=?", ("p1",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.decision_recorded'"
        ).fetchone()[0] == 0


def test_valid_handoff_still_authorizes_and_replays_exact_decision(tmp_path):
    store, projects, decisions = _setup(tmp_path)
    approved = _approve(decisions)
    replay = _approve(ProductDecisionRepository(store))
    assert approved == replay
    assert approved.evidence_package_ids == ("research-1",)
    assert projects.get("p1").row_version == 1
