from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)
from nika_core.product_project_lifecycle import ProductProjectLifecycleService


def test_restart_history_rejects_raw_non_integer_project_row_version(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p-aud01-raw-version",
        name="AUD01 raw version oracle",
        spec=ProductProjectSpec(
            goal="Reject raw durable version coercion",
            desired_outcome="Lifecycle history fails closed on REAL row_version",
        ),
        idempotency_key="create:p-aud01-raw-version",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_projects SET row_version=? WHERE project_id=?",
            (0.5, "p-aud01-raw-version"),
        )

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    with pytest.raises(ProductProjectError, match="row_version"):
        restarted.history("p-aud01-raw-version")
