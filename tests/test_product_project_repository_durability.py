from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)


def _spec(goal: str = "Build durable accessible product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A restart-safe versioned product",
    )


def _repo(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = ProductProjectRepository(store)
    project = repo.create(
        project_id="p1",
        name="Durable product",
        spec=_spec(),
        idempotency_key="create:p1",
    )
    return store, repo, project


def test_repository_missing_project_remains_not_found(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    with pytest.raises(KeyError, match="missing-project"):
        ProductProjectRepository(store).get("missing-project")


def test_repository_fails_closed_when_current_spec_row_is_missing_after_restart(tmp_path) -> None:
    store, _, _ = _repo(tmp_path)
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
        ProductProjectRepository(restarted).get("p1")


def test_repository_fails_closed_on_dangling_current_spec_version(tmp_path) -> None:
    store, repo, _ = _repo(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET current_spec_version=2 WHERE project_id='p1'"
        )

    with pytest.raises(
        ProductProjectError,
        match=r"current ProductProject specification is missing: project_id=p1, spec_version=2",
    ):
        repo.get("p1")


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("current_spec_version", 1.5, "current_spec_version"),
        ("row_version", 0.5, "row_version"),
    ],
)
def test_repository_rejects_non_integer_durable_project_versions(
    tmp_path, column: str, value: float, message: str
) -> None:
    store, repo, _ = _repo(tmp_path)
    with store.connection() as conn:
        conn.execute(f"UPDATE product_projects SET {column}=? WHERE project_id='p1'", (value,))

    with pytest.raises(ProductProjectError, match=message):
        repo.get("p1")


@pytest.mark.parametrize("expected_row_version", [True, 0.0, "0", -1])
def test_update_spec_rejects_non_integer_expected_version_without_mutation(
    tmp_path, expected_row_version: object
) -> None:
    store, repo, _ = _repo(tmp_path)

    with pytest.raises(ProductProjectError, match="expected_row_version"):
        repo.update_spec(
            "p1",
            _spec("changed"),
            expected_row_version=expected_row_version,  # type: ignore[arg-type]
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT current_spec_version,row_version FROM product_projects WHERE project_id='p1'"
        ).fetchone()
        assert tuple(row) == (1, 0)
        count = conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0]
        assert count == 1


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("current_spec_version", 1.5, "current_spec_version"),
        ("row_version", 0.5, "row_version"),
    ],
)
def test_update_spec_rejects_corrupt_durable_versions_before_mutation(
    tmp_path, column: str, value: float, message: str
) -> None:
    store, repo, _ = _repo(tmp_path)
    with store.connection() as conn:
        conn.execute(f"UPDATE product_projects SET {column}=? WHERE project_id='p1'", (value,))

    with pytest.raises(ProductProjectError, match=message):
        repo.update_spec("p1", _spec("changed"), expected_row_version=0)

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0]
        assert count == 1


def test_spec_history_rejects_missing_and_non_integer_rows(tmp_path) -> None:
    store, repo, _ = _repo(tmp_path)
    with store.connection() as conn:
        conn.execute("DELETE FROM product_project_specs WHERE project_id='p1'")
    with pytest.raises(ProductProjectError, match="history is missing"):
        repo.spec_history("p1")

    replacement = _spec().to_dict()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
            "VALUES (?,?,?,?)",
            ("p1", 1.5, json.dumps(replacement), "2026-08-23T00:00:00+00:00"),
        )
    with pytest.raises(ProductProjectError, match="spec_version"):
        repo.spec_history("p1")


def test_repository_rejects_boolean_spec_lineage_after_persistence_tamper(tmp_path) -> None:
    store, repo, project = _repo(tmp_path)
    updated = repo.update_spec(
        "p1",
        _spec("second version"),
        expected_row_version=project.row_version,
    )
    assert updated.spec_version == 2

    with store.connection() as conn:
        row = conn.execute(
            "SELECT spec_json FROM product_project_specs "
            "WHERE project_id='p1' AND spec_version=2"
        ).fetchone()
        payload = json.loads(row[0])
        payload["supersedes_spec_version"] = True
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='p1' AND spec_version=2",
            (json.dumps(payload),),
        )

    with pytest.raises(ProductProjectError, match="supersedes_spec_version"):
        repo.get("p1")
    with pytest.raises(ProductProjectError, match="supersedes_spec_version"):
        repo.spec_history("p1")


def test_repository_normalizes_malformed_spec_json_to_domain_error(tmp_path) -> None:
    store, repo, _ = _repo(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_specs SET spec_json='{' "
            "WHERE project_id='p1' AND spec_version=1"
        )

    with pytest.raises(
        ProductProjectError,
        match="invalid current ProductProject specification JSON",
    ):
        repo.get("p1")
