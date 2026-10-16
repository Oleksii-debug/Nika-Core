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
        name="PF1 admission",
        spec=ProductProjectSpec(
            goal="Admit only canonical persisted research handoffs",
            desired_outcome="Owner approval intent is bound to valid evidence",
        ),
        idempotency_key="create:p1",
    )
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            "research-1",
            (
                EvidenceRef(
                    "evidence-1",
                    "research://source-1",
                    "Relevant non-secret evidence",
                ),
            ),
        ),
        (
            ProductOption(
                "option-1",
                "Option",
                "Summary",
                ("research-1",),
            ),
        ),
    )
    decision = ProductDecision(
        "decision-1",
        "option-1",
        ProductDecisionState.APPROVED,
        "Evidence supports the option",
        "user://owner",
    )
    return store, projects, ProductDecisionRepository(store), decision


def _approval_intent(decisions, decision):
    return decisions.approval_intent(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:approve:1",
    )


def _corrupt(payload, case):
    if case == "json":
        return "{"
    if case == "array":
        return "[]"
    if case == "deep-json":
        return '{"nested":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    if case == "exponent-overflow":
        return json.dumps(payload, ensure_ascii=False)[:-1] + ', "unused": 1e999}'
    if case == "wrong-package":
        payload["package_id"] = "other"
    elif case == "invalid-other-option":
        payload["options"].append(False)
    elif case == "duplicate-options":
        payload["options"].append(dict(payload["options"][0]))
    elif case == "empty-evidence":
        payload["evidence"] = []
    elif case == "invalid-evidence-entry":
        payload["evidence"][0] = None
    elif case == "surrogate-evidence-id":
        payload["evidence"][0]["evidence_id"] = "\ud800"
        return json.dumps(payload, ensure_ascii=True)
    elif case == "surrogate-provenance":
        payload["evidence"][0]["provenance_ref"] = "\ud800"
        return json.dumps(payload, ensure_ascii=True)
    elif case == "duplicate-evidence":
        payload["evidence"].append(dict(payload["evidence"][0]))
    elif case == "invalid-option-id":
        payload["options"][0]["option_id"] = False
    elif case == "missing-package-ids":
        del payload["options"][0]["evidence_package_ids"]
    elif case == "duplicate-package-ids":
        payload["options"][0]["evidence_package_ids"].append("research-1")
    elif case == "foreign-package":
        payload["options"][0]["evidence_package_ids"] = ["research-1", "research-2"]
    elif case == "nonfinite":
        payload["untrusted"] = float("nan")
    elif case == "duplicate-key":
        raw = json.dumps(payload, ensure_ascii=False)
        return raw.replace(
            '"package_id":',
            '"package_id": "shadow", "package_id":',
            1,
        )
    else:
        raise AssertionError(case)
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "case",
    (
        "json",
        "array",
        "deep-json",
        "exponent-overflow",
        "wrong-package",
        "invalid-other-option",
        "duplicate-options",
        "empty-evidence",
        "invalid-evidence-entry",
        "surrogate-evidence-id",
        "surrogate-provenance",
        "duplicate-evidence",
        "invalid-option-id",
        "missing-package-ids",
        "duplicate-package-ids",
        "foreign-package",
        "nonfinite",
        "duplicate-key",
    ),
)
def test_corrupt_handoff_cannot_mint_owner_approval_intent(tmp_path, case):
    store, projects, decisions, decision = _setup(tmp_path)
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

    with pytest.raises(ProductProjectError):
        _approval_intent(decisions, decision)

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions WHERE project_id=?",
            ("p1",),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.decision_recorded'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("column", ("package_id", "payload_json"))
def test_invalid_utf8_sqlite_text_fails_closed_before_approval_intent(
    tmp_path,
    column,
):
    store, projects, decisions, decision = _setup(tmp_path)
    update_sql = {
        "package_id": (
            "UPDATE product_research_handoffs SET package_id=CAST(X'80' AS TEXT) "
            "WHERE project_id='p1' AND package_id='research-1'"
        ),
        "payload_json": (
            "UPDATE product_research_handoffs SET payload_json=CAST(X'80' AS TEXT) "
            "WHERE project_id='p1' AND package_id='research-1'"
        ),
    }[column]
    with store.connection() as conn:
        conn.execute(update_sql)
        row = conn.execute(
            f"SELECT typeof({column}) AS storage_type,"
            f"length(CAST({column} AS BLOB)) AS byte_length "
            "FROM product_research_handoffs WHERE project_id='p1'"
        ).fetchone()
        assert row is not None
        assert row["storage_type"] == "text"
        assert row["byte_length"] == 1

    with pytest.raises(ProductProjectError, match="malformed"):
        _approval_intent(decisions, decision)

    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions WHERE project_id='p1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency"
        ).fetchone()[0] == 0


def test_oversized_handoff_fails_before_json_decode(tmp_path, monkeypatch):
    store, projects, decisions, decision = _setup(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id=? AND package_id=?",
            ("p1", "research-1"),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["padding"] = "x" * (1024 * 1024)
        oversized = json.dumps(payload, ensure_ascii=False)
        assert len(oversized.encode("utf-8")) > 1024 * 1024
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id=? AND package_id=?",
            (oversized, "p1", "research-1"),
        )

    def unexpected_json_loads(*_args, **_kwargs):
        raise AssertionError("oversized handoff reached json.loads")

    with monkeypatch.context() as patch:
        patch.setattr(
            "nika_core.product_decisions.json.loads",
            unexpected_json_loads,
        )
        with pytest.raises(ProductProjectError, match="malformed"):
            _approval_intent(decisions, decision)

    assert projects.get("p1").row_version == 0



def test_approval_intent_reads_handoff_on_one_sqlite_snapshot(
    tmp_path,
    monkeypatch,
):
    _store, projects, decisions, decision = _setup(tmp_path)
    original_option = decisions._option_evidence_conn
    original_fingerprint = decisions._evidence_authority_fingerprint_conn
    observed: list[str] = []

    def checked_option(conn, project_id, option_id):
        assert conn.in_transaction
        observed.append("option")
        return original_option(conn, project_id, option_id)

    def checked_fingerprint(conn, project_id, evidence_package_ids):
        assert conn.in_transaction
        observed.append("fingerprint")
        return original_fingerprint(conn, project_id, evidence_package_ids)

    monkeypatch.setattr(decisions, "_option_evidence_conn", checked_option)
    monkeypatch.setattr(
        decisions,
        "_evidence_authority_fingerprint_conn",
        checked_fingerprint,
    )

    intent = _approval_intent(decisions, decision)

    assert observed == ["option", "fingerprint"]
    assert intent.action_id == "product_project.decision.approve"
    assert projects.get("p1").row_version == 0

def test_valid_handoff_still_builds_exact_owner_approval_intent(tmp_path):
    store, projects, decisions, decision = _setup(tmp_path)
    intent = _approval_intent(decisions, decision)

    assert intent.action_id == "product_project.decision.approve"
    assert intent.project_id == "p1"
    assert intent.resource == "option-1"
    assert intent.arguments["expected_row_version"] == 0
    assert len(intent.arguments["evidence_fingerprint"]) == 64
    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions WHERE project_id='p1'"
        ).fetchone()[0] == 0
