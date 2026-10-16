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
    ResearchEvidencePackage,
)


class ExplodingText(str):
    def strip(self, *_args, **_kwargs):
        raise AssertionError("behavioral string strip() executed")

    def encode(self, *_args, **_kwargs):
        raise AssertionError("behavioral string encode() executed")


class ProductDecisionSubclass(ProductDecision):
    pass


class IntegerSubclass(int):
    pass


def _setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="PF1 carrier admission",
        spec=ProductProjectSpec(
            goal="Preserve exact product-decision authority",
            desired_outcome="Untrusted carriers fail before durable mutation",
        ),
        idempotency_key="create:p1",
    )
    projects.record_research_handoff(
        "p1",
        ResearchEvidencePackage(
            package_id="research-1",
            evidence=(
                EvidenceRef(
                    evidence_id="evidence-1",
                    provenance_ref="research://carrier/1",
                    claim="Non-secret carrier regression evidence",
                ),
            ),
        ),
        (
            ProductOption(
                option_id="option-1",
                title="Option",
                summary="Summary",
                evidence_package_ids=("research-1",),
            ),
        ),
    )
    return store, projects, ProductDecisionRepository(store)


def _decision(**changes):
    values = {
        "decision_id": "decision-1",
        "option_id": "option-1",
        "state": ProductDecisionState.PROPOSED,
        "rationale": "Review before owner finalization",
        "decided_by_ref": "workflow://proposal",
    }
    values.update(changes)
    return ProductDecision(**values)


def _assert_no_decision_mutation(
    store: SQLiteStore,
    projects: ProductProjectRepository,
) -> None:
    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions WHERE project_id='p1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE operation_kind='product_decision.record'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "field",
    ("decision_id", "option_id", "rationale", "decided_by_ref"),
)
def test_behavioral_decision_text_is_rejected_without_virtual_calls(
    tmp_path: Path,
    field: str,
) -> None:
    store, projects, decisions = _setup(tmp_path)
    decision = _decision(**{field: ExplodingText("deceptive")})

    with pytest.raises(ProductProjectError, match="non-empty text"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key=f"decision:carrier:{field}",
        )

    _assert_no_decision_mutation(store, projects)


def test_behavioral_request_text_is_rejected_before_sql(tmp_path: Path) -> None:
    store, projects, decisions = _setup(tmp_path)

    with pytest.raises(ProductProjectError, match="project_id"):
        decisions.list(ExplodingText("p1"))

    with pytest.raises(ProductProjectError, match="idempotency_key"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key=ExplodingText("decision:key"),
        )

    _assert_no_decision_mutation(store, projects)


def test_product_decision_requires_exact_carrier_and_enum_state(
    tmp_path: Path,
) -> None:
    store, projects, decisions = _setup(tmp_path)
    subclass = ProductDecisionSubclass(
        "decision-1",
        "option-1",
        ProductDecisionState.PROPOSED,
        "Review",
        "workflow://proposal",
    )
    with pytest.raises(ProductProjectError, match="exact ProductDecision"):
        decisions.record(
            "p1",
            subclass,
            expected_row_version=0,
            idempotency_key="decision:subclass",
        )

    tampered = _decision()
    object.__setattr__(tampered, "state", "proposed")
    with pytest.raises(
        ProductProjectError,
        match="state must be ProductDecisionState",
    ):
        decisions.record(
            "p1",
            tampered,
            expected_row_version=0,
            idempotency_key="decision:tampered-state",
        )

    _assert_no_decision_mutation(store, projects)


@pytest.mark.parametrize(
    "bad_version",
    (True, IntegerSubclass(0), 0.0, 1 << 63),
)
def test_row_version_requires_exact_sqlite_integer(
    tmp_path: Path,
    bad_version: object,
) -> None:
    store, projects, decisions = _setup(tmp_path)

    with pytest.raises(ProductProjectError, match="exact integer"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=bad_version,
            idempotency_key=f"decision:version:{type(bad_version).__name__}",
        )

    _assert_no_decision_mutation(store, projects)


@pytest.mark.parametrize(
    ("column", "sql_value", "message"),
    (
        ("rationale", "X'80'", "rationale"),
        ("created_at", "X'80'", "created_at"),
        ("state", "'not-a-state'", "state is invalid"),
        (
            "evidence_package_ids_json",
            "'\"research-1\"'",
            "non-empty list",
        ),
    ),
)
def test_corrupt_durable_decision_rows_fail_closed(
    tmp_path: Path,
    column: str,
    sql_value: str,
    message: str,
) -> None:
    store, _projects, decisions = _setup(tmp_path)
    decisions.record(
        "p1",
        _decision(),
        expected_row_version=0,
        idempotency_key="decision:stored",
    )

    with store.connection() as conn:
        conn.execute(
            f"UPDATE product_decisions SET {column}={sql_value} "
            "WHERE project_id='p1' AND decision_id='decision-1'"
        )

    with pytest.raises(ProductProjectError, match=message):
        decisions.get("p1", "decision-1")


def test_corrupt_replay_text_fails_closed_before_replay(tmp_path: Path) -> None:
    store, projects, decisions = _setup(tmp_path)
    decisions.record(
        "p1",
        _decision(),
        expected_row_version=0,
        idempotency_key="decision:replay",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_mutation_idempotency "
            "SET operation_kind=X'80' WHERE operation_key='decision:replay'"
        )

    with pytest.raises(ProductProjectError, match="operation_kind"):
        decisions.record(
            "p1",
            _decision(),
            expected_row_version=0,
            idempotency_key="decision:replay",
        )

    assert projects.get("p1").row_version == 1
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_decisions "
            "WHERE project_id='p1' AND decision_id='decision-1'"
        ).fetchone()[0] == 1

def test_durable_replay_verifies_evidence_on_one_sqlite_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, projects, decisions = _setup(tmp_path)
    decision = _decision()
    stored = decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:snapshot-replay",
    )
    original_replay = decisions._replay_conn
    observed: list[bool] = []

    def checked_replay(conn, *args, **kwargs):
        observed.append(conn.in_transaction)
        assert conn.in_transaction
        return original_replay(conn, *args, **kwargs)

    monkeypatch.setattr(decisions, "_replay_conn", checked_replay)

    replay = decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:snapshot-replay",
    )

    assert replay == stored
    assert observed == [True]
    assert projects.get("p1").row_version == 1

