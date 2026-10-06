from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)
from nika_core.product_project_lifecycle import ProductProjectLifecycleService


def _create_project(store: SQLiteStore, project_id: str = "p-core-version") -> None:
    ProductProjectRepository(store).create(
        project_id=project_id,
        name="Core version integrity",
        spec=ProductProjectSpec(
            goal="Keep ProductProject durable version identity exact",
            desired_outcome="Corrupt numeric identities fail closed before normalization",
        ),
        idempotency_key=f"create:{project_id}",
    )


def test_raw_real_row_version_fails_at_repository_and_lifecycle_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _create_project(store)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET row_version=? WHERE project_id=?",
            (0.5, "p-core-version"),
        )

    restarted_store = SQLiteStore(store.path)
    with pytest.raises(ProductProjectError, match="row_version"):
        ProductProjectRepository(restarted_store).get("p-core-version")
    with pytest.raises(ProductProjectError, match="row_version"):
        ProductProjectLifecycleService(restarted_store).history("p-core-version")


def test_update_spec_rejects_boolean_expected_row_version_without_mutation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _create_project(store)
    repository = ProductProjectRepository(store)

    with pytest.raises(ProductProjectError, match="expected_row_version"):
        repository.update_spec(
            "p-core-version",
            ProductProjectSpec(goal="Changed", desired_outcome="Must not persist"),
            expected_row_version=True,
        )

    project = repository.get("p-core-version")
    assert project.row_version == 0
    assert project.spec_version == 1
    assert project.spec.goal == "Keep ProductProject durable version identity exact"


def test_boolean_spec_lineage_version_is_rejected() -> None:
    with pytest.raises(ProductProjectError, match="supersedes_spec_version"):
        ProductProjectSpec(
            goal="Invalid lineage",
            desired_outcome="Rejected",
            supersedes_spec_version=True,
        )


def test_valid_integer_versions_remain_supported(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _create_project(store)
    repository = ProductProjectRepository(store)

    updated = repository.update_spec(
        "p-core-version",
        ProductProjectSpec(goal="Version two", desired_outcome="Valid exact integer lineage"),
        expected_row_version=0,
        change_reason="Valid revision",
    )

    assert updated.row_version == 1
    assert updated.spec_version == 2
    assert updated.spec.supersedes_spec_version == 1
