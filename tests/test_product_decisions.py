from __future__ import annotations

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
    StaleProjectVersionError,
)
from nika_core.product_project_schema import PRODUCT_PROJECT_MIGRATIONS


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build accessible expense app",
        desired_outcome="A tested durable product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Keyboard operation",
                ("All primary actions keyboard reachable",),
            ),
        ),
    )


def _repos(tmp_path) -> tuple[SQLiteStore, ProductProjectRepository, ProductDecisionRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )
    return store, projects, ProductDecisionRepository(store)


def _handoff(
    projects: ProductProjectRepository,
    *,
    package_id: str = "research-1",
    option_id: str = "option-1",
    extra_package_ids: tuple[str, ...] = (),
) -> None:
    package = ResearchEvidencePackage(
        package_id,
        (
            EvidenceRef(
                f"ev:{package_id}",
                f"research://{package_id}/claim/1",
                "Gap",
            ),
        ),
    )
    projects.record_research_handoff(
        "p1",
        package,
        (
            ProductOption(
                option_id,
                option_id,
                "Summary",
                (package_id, *extra_package_ids),
            ),
        ),
    )


def _decision(
    *,
    decision_id: str = "decision-1",
    option_id: str = "option-1",
    state: ProductDecisionState = ProductDecisionState.APPROVED,
    rationale: str = "Best evidence-backed option",
) -> ProductDecision:
    return ProductDecision(
        decision_id=decision_id,
        option_id=option_id,
        state=state,
        rationale=rationale,
        decided_by_ref="user://owner",
    )


def test_decision_is_durable_idempotent_and_restart_safe(tmp_path) -> None:
    store, projects, decisions = _repos(tmp_path)
    _handoff(projects)

    stored = decisions.record(
        "p1",
        _decision(),
        expected_row_version=0,
        idempotency_key="decision:approve:1",
    )
    replay = decisions.record(
        "p1",
        _decision(),
        expected_row_version=0,
        idempotency_key="decision:approve:1",
    )

    assert replay == stored
    assert stored.decision_version == 1
    assert stored.evidence_package_ids == ("research-1",)
    assert projects.get("p1").row_version == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductDecisionRepository(restarted_store)
    assert restarted.get("p1", "decision-1") == stored
    assert restarted.list("p1") == (stored,)


def test_decision_requires_exact_unambiguous_recorded_option(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    with pytest.raises(ProductProjectError, match="unknown product option"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:unknown",
        )
    assert projects.get("p1").row_version == 0

    _handoff(projects, package_id="research-1", option_id="shared")
    _handoff(projects, package_id="research-2", option_id="shared")
    with pytest.raises(ProductProjectError, match="ambiguous product option"):
        decisions.record(
            "p1",
            _decision(option_id="shared"),
            expected_row_version=0,
            idempotency_key="decision:ambiguous",
        )
    assert projects.get("p1").row_version == 0


def test_decision_rejects_option_with_missing_evidence_package(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    _handoff(
        projects,
        extra_package_ids=("research-missing",),
    )
    with pytest.raises(ProductProjectError, match="unknown evidence package"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:missing-evidence",
        )
    assert projects.get("p1").row_version == 0


def test_proposed_decision_can_finalize_once_and_history_survives(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    _handoff(projects)
    proposed = decisions.record(
        "p1",
        _decision(
            state=ProductDecisionState.PROPOSED,
            rationale="Needs owner confirmation",
        ),
        expected_row_version=0,
        idempotency_key="decision:proposed",
    )
    approved = decisions.record(
        "p1",
        _decision(),
        expected_row_version=1,
        idempotency_key="decision:approved",
    )

    assert proposed.decision_version == 1
    assert approved.decision_version == 2
    assert [item.decision.state for item in decisions.history("p1", "decision-1")] == [
        ProductDecisionState.PROPOSED,
        ProductDecisionState.APPROVED,
    ]
    with pytest.raises(ProductProjectError, match="immutable"):
        decisions.record(
            "p1",
            _decision(
                state=ProductDecisionState.REJECTED,
                rationale="Changed mind",
            ),
            expected_row_version=2,
            idempotency_key="decision:rewrite-final",
        )
    assert projects.get("p1").row_version == 2


def test_decision_option_identity_cannot_change_during_finalization(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    _handoff(projects, package_id="research-1", option_id="option-1")
    _handoff(projects, package_id="research-2", option_id="option-2")
    decisions.record(
        "p1",
        _decision(
            state=ProductDecisionState.PROPOSED,
            rationale="Review",
        ),
        expected_row_version=0,
        idempotency_key="decision:proposed:fixed-option",
    )
    with pytest.raises(ProductProjectError, match="option cannot change"):
        decisions.record(
            "p1",
            _decision(option_id="option-2"),
            expected_row_version=1,
            idempotency_key="decision:changed-option",
        )
    assert projects.get("p1").row_version == 1


def test_only_one_current_option_can_be_approved(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    _handoff(projects, package_id="research-1", option_id="option-1")
    _handoff(projects, package_id="research-2", option_id="option-2")
    decisions.record(
        "p1",
        _decision(decision_id="decision-1", option_id="option-1"),
        expected_row_version=0,
        idempotency_key="decision:1",
    )

    with pytest.raises(ProductProjectError, match="already has approved option"):
        decisions.record(
            "p1",
            _decision(decision_id="decision-2", option_id="option-2"),
            expected_row_version=1,
            idempotency_key="decision:2",
        )
    assert projects.get("p1").row_version == 1


def test_requirement_link_is_explicit_versioned_and_idempotent(tmp_path) -> None:
    store, projects, decisions = _repos(tmp_path)
    _handoff(projects)
    decisions.record(
        "p1",
        _decision(),
        expected_row_version=0,
        idempotency_key="decision:approve",
    )

    linked = decisions.link_requirement(
        "p1",
        requirement_id="req-1",
        decision_id="decision-1",
        expected_row_version=1,
    )
    replay = decisions.link_requirement(
        "p1",
        requirement_id="req-1",
        decision_id="decision-1",
        expected_row_version=1,
    )
    assert linked.spec_version == 2
    assert linked.row_version == 2
    assert replay == linked
    assert linked.spec.requirements[0].decision_ids == ("decision-1",)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    recovered = ProductProjectRepository(restarted_store).get("p1")
    assert recovered.spec.requirements[0].decision_ids == ("decision-1",)


def test_requirement_link_rejects_proposed_and_stale_decisions(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    _handoff(projects)
    decisions.record(
        "p1",
        _decision(
            state=ProductDecisionState.PROPOSED,
            rationale="Review",
        ),
        expected_row_version=0,
        idempotency_key="decision:proposed:requirement",
    )
    with pytest.raises(ProductProjectError, match="requires approved"):
        decisions.link_requirement(
            "p1",
            requirement_id="req-1",
            decision_id="decision-1",
            expected_row_version=1,
        )
    with pytest.raises(StaleProjectVersionError):
        decisions.link_requirement(
            "p1",
            requirement_id="req-1",
            decision_id="decision-1",
            expected_row_version=0,
        )
    assert projects.get("p1").spec_version == 1


def test_decision_stale_write_and_idempotency_conflict_fail_closed(tmp_path) -> None:
    _, projects, decisions = _repos(tmp_path)
    _handoff(projects)
    stored = decisions.record(
        "p1",
        _decision(
            state=ProductDecisionState.PROPOSED,
            rationale="Review",
        ),
        expected_row_version=0,
        idempotency_key="decision:key",
    )
    assert stored.decision_version == 1

    with pytest.raises(ProductProjectError, match="different mutation input"):
        decisions.record(
            "p1",
            _decision(state=ProductDecisionState.APPROVED),
            expected_row_version=1,
            idempotency_key="decision:key",
        )
    with pytest.raises(StaleProjectVersionError):
        decisions.record(
            "p1",
            _decision(
                decision_id="decision-2",
                state=ProductDecisionState.REJECTED,
            ),
            expected_row_version=0,
            idempotency_key="decision:stale",
        )
    assert projects.get("p1").row_version == 1


def test_product_project_schema_v1_upgrades_without_data_loss(tmp_path) -> None:
    db = tmp_path / "nika-v1.db"
    store = SQLiteStore(db)
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'existing')"
        )
        conn.execute(
            """CREATE TABLE audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE TABLE product_project_schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for statement in PRODUCT_PROJECT_MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO product_project_schema_migrations(version, applied_at) "
            "VALUES (1, 'existing')"
        )
        conn.execute(
            "INSERT INTO product_projects(project_id,name,current_spec_version,row_version,"
            "status,created_at,updated_at) VALUES ('p1','Existing',1,0,'active','x','x')"
        )
        conn.execute(
            "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
            "VALUES ('p1',1,?, 'x')",
            (
                '{"goal":"g","desired_outcome":"o","hypothesis":"",'
                '"requirements":[],"architecture_decision_refs":[],"repository_refs":[],'
                '"team_refs":[],"artifact_refs":[],"build_refs":[],"release_refs":[],'
                '"deployment_refs":[],"incident_refs":[],"credential_refs":[],'
                '"budget":{},"risk":{},"compliance":{}}',
            ),
        )

    store.initialize()
    recovered = ProductProjectRepository(store).get("p1")
    assert recovered.name == "Existing"
    with store.connection() as conn:
        version = conn.execute(
            "SELECT MAX(version) FROM product_project_schema_migrations"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert version == 2
    assert {"product_decisions", "product_project_mutation_idempotency"} <= tables
