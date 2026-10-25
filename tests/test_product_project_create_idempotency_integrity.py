from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)


def _spec(goal: str) -> ProductProjectSpec:
    return ProductProjectSpec(goal=goal, desired_outcome="Durable product")


def test_create_idempotency_exact_replay_survives_restart(tmp_path) -> None:
    db = tmp_path / "nika.db"
    store = SQLiteStore(db)
    store.initialize()
    first = ProductProjectRepository(store).create(
        project_id="project-a",
        name="Project A",
        spec=_spec("Build A"),
        idempotency_key="create:project-a",
    )

    restarted = SQLiteStore(db)
    restarted.initialize()
    replayed = ProductProjectRepository(restarted).create(
        project_id="project-a",
        name="Project A",
        spec=_spec("Build A"),
        idempotency_key="create:project-a",
    )

    assert replayed == first
    assert replayed.project_id == "project-a"


def test_create_idempotency_rejects_durable_project_pointer_substitution(tmp_path) -> None:
    db = tmp_path / "nika.db"
    store = SQLiteStore(db)
    store.initialize()
    repo = ProductProjectRepository(store)
    repo.create(
        project_id="project-a",
        name="Project A",
        spec=_spec("Build A"),
        idempotency_key="create:project-a",
    )
    repo.create(
        project_id="project-b",
        name="Project B",
        spec=_spec("Build B"),
        idempotency_key="create:project-b",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_idempotency SET project_id=? WHERE operation_key=?",
            ("project-b", "create:project-a"),
        )

    restarted = SQLiteStore(db)
    restarted.initialize()
    restarted_repo = ProductProjectRepository(restarted)
    with pytest.raises(ProductProjectError, match="project identity mismatch"):
        restarted_repo.create(
            project_id="project-a",
            name="Project A",
            spec=_spec("Build A"),
            idempotency_key="create:project-a",
        )

    assert restarted_repo.get("project-a").project_id == "project-a"
    assert restarted_repo.get("project-b").project_id == "project-b"
