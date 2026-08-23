from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)


def _spec(hypothesis: str = "initial") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Keep specification history authoritative",
        desired_outcome="Durable PF12 lineage fails closed on corruption",
        hypothesis=hypothesis,
    )


def _repo(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = ProductProjectRepository(store)
    project = repo.create(
        project_id="p-history",
        name="History integrity",
        spec=_spec(),
        idempotency_key="create:p-history",
    )
    return store, repo, project


def test_spec_history_rejects_missing_current_revision(tmp_path) -> None:
    store, repo, _ = _repo(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET current_spec_version=2,row_version=1 "
            "WHERE project_id='p-history'"
        )

    with pytest.raises(ProductProjectError, match="not contiguous through current version"):
        repo.spec_history("p-history")


def test_spec_history_rejects_revision_ahead_of_current_pointer(tmp_path) -> None:
    store, repo, project = _repo(tmp_path)
    payload = _spec("ahead").to_dict()
    payload["supersedes_spec_version"] = 1
    payload["revision_reason"] = "forged ahead revision"
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
            "VALUES (?,?,?,?)",
            ("p-history", 2, json.dumps(payload), project.updated_at),
        )

    with pytest.raises(ProductProjectError, match="not contiguous through current version"):
        repo.spec_history("p-history")


def test_spec_history_rejects_gap_before_current_revision(tmp_path) -> None:
    store, repo, project = _repo(tmp_path)
    second = repo.update_spec(
        "p-history",
        _spec("second"),
        expected_row_version=project.row_version,
        idempotency_key="spec:second",
        change_reason="second",
    )
    third = repo.update_spec(
        "p-history",
        _spec("third"),
        expected_row_version=second.row_version,
        idempotency_key="spec:third",
        change_reason="third",
    )
    assert third.spec_version == 3
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM product_project_specs "
            "WHERE project_id='p-history' AND spec_version=2"
        )

    with pytest.raises(ProductProjectError, match="not contiguous through current version"):
        repo.spec_history("p-history")


def test_spec_history_rejects_explicit_parent_mismatch(tmp_path) -> None:
    store, repo, project = _repo(tmp_path)
    repo.update_spec(
        "p-history",
        _spec("second"),
        expected_row_version=project.row_version,
        idempotency_key="spec:second",
        change_reason="second",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT spec_json FROM product_project_specs "
            "WHERE project_id='p-history' AND spec_version=2"
        ).fetchone()
        payload = json.loads(row["spec_json"])
        payload["supersedes_spec_version"] = 9
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='p-history' AND spec_version=2",
            (json.dumps(payload),),
        )

    with pytest.raises(ProductProjectError, match=r"supersedes 9, expected 1"):
        repo.spec_history("p-history")


def test_spec_history_rejects_explicit_parent_without_reason(tmp_path) -> None:
    store, repo, project = _repo(tmp_path)
    repo.update_spec(
        "p-history",
        _spec("second"),
        expected_row_version=project.row_version,
        idempotency_key="spec:second",
        change_reason="second",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT spec_json FROM product_project_specs "
            "WHERE project_id='p-history' AND spec_version=2"
        ).fetchone()
        payload = json.loads(row["spec_json"])
        payload["revision_reason"] = ""
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='p-history' AND spec_version=2",
            (json.dumps(payload),),
        )

    with pytest.raises(ProductProjectError, match="has no revision reason"):
        repo.spec_history("p-history")


def test_spec_history_preserves_legacy_sequential_parent_compatibility(tmp_path) -> None:
    store, repo, project = _repo(tmp_path)
    updated = repo.update_spec(
        "p-history",
        _spec("second"),
        expected_row_version=project.row_version,
        idempotency_key="spec:second",
        change_reason="second",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT spec_json FROM product_project_specs "
            "WHERE project_id='p-history' AND spec_version=2"
        ).fetchone()
        payload = json.loads(row["spec_json"])
        payload["supersedes_spec_version"] = None
        payload["revision_reason"] = ""
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='p-history' AND spec_version=2",
            (json.dumps(payload),),
        )

    history = repo.spec_history("p-history")
    assert updated.spec_version == 2
    assert [revision.spec_version for revision in history] == [1, 2]
    assert history[1].supersedes_spec_version == 1
    assert history[1].change_reason == "legacy sequential specification"
