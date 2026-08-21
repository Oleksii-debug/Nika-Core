from __future__ import annotations

import sqlite3

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService
from nika_core.product_project_history_integrity import (
    ProductProjectHistoricalIntegrityService,
)


def test_archive_holds_writer_guard_across_integrity_validation(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="project-1",
        name="Archive guard",
        spec=ProductProjectSpec(goal="goal", desired_outcome="outcome"),
        idempotency_key="create:project-1",
    )

    original_validate = ProductProjectHistoricalIntegrityService.validate
    contender_was_blocked = False

    def validate_under_archive_guard(
        self,
        project_id: str,
        *,
        expected_spec_version: int | None = None,
        expected_row_version: int | None = None,
    ):
        nonlocal contender_was_blocked
        contender = sqlite3.connect(store.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
            contender_was_blocked = True
        finally:
            contender.close()
        return original_validate(
            self,
            project_id,
            expected_spec_version=expected_spec_version,
            expected_row_version=expected_row_version,
        )

    monkeypatch.setattr(
        ProductProjectHistoricalIntegrityService,
        "validate",
        validate_under_archive_guard,
    )

    archive = ProductProjectHistoryArchiveService(store).build("project-1")

    assert contender_was_blocked
    assert archive.summary.project_id == "project-1"
    assert archive.summary.research_package_count == 0
