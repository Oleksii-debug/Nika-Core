from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import ProductProjectError
from nika_core.product_project_integrity import ProductProjectIntegrityService

import test_product_project_integrity as baseline


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("current_spec_version", 1.5, "spec version metadata"),
        ("row_version", 0.5, "row version metadata"),
    ],
)
def test_integrity_rejects_non_integer_project_version_metadata(
    tmp_path, column: str, value: float, message: str
) -> None:
    store, _, _ = baseline._repos(tmp_path)
    with store.connection() as conn:
        conn.execute(f"UPDATE product_projects SET {column}=? WHERE project_id='p1'", (value,))

    with pytest.raises(ProductProjectError, match=message):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_rejects_non_integer_historical_spec_version(tmp_path) -> None:
    store, projects, project = baseline._repos(tmp_path)
    projects.update_spec(
        "p1",
        replace(project.spec, hypothesis="second revision"),
        expected_row_version=project.row_version,
        change_reason="second revision",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_specs SET spec_version=1.5 "
            "WHERE project_id='p1' AND spec_version=1"
        )

    with pytest.raises(ProductProjectError, match="specification version"):
        ProductProjectIntegrityService(store).validate("p1")


def test_integrity_rejects_non_integer_decision_version(tmp_path) -> None:
    store, projects, project = baseline._repos(tmp_path)
    baseline._approve(
        projects,
        ProductDecisionRepository(store),
        row_version=project.row_version,
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET decision_version=1.5 "
            "WHERE project_id='p1' AND decision_id='decision-1'"
        )

    with pytest.raises(ProductProjectError, match="product decision version"):
        ProductProjectIntegrityService(store).validate("p1")
