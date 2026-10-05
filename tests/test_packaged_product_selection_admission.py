from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_packaged_journey import (
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    packaged_product_reopen_target,
)


def _selection(tmp_path: Path) -> tuple[SQLiteStore, PackagedProductSelectionStore]:
    store = SQLiteStore(tmp_path / "збережений вибір з пробілами.db")
    store.initialize()
    return store, PackagedProductSelectionStore(store)


@pytest.mark.parametrize(
    "stored",
    [
        " product-existing ",
        "product-existing\x00",
        "product-\u202eexisting",
        sqlite3.Binary(b"product-existing"),
    ],
)
def test_corrupt_persisted_selection_is_not_coerced_or_modified(
    tmp_path: Path, stored: object
) -> None:
    store, selection = _selection(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO packaged_product_selection(slot, project_id) VALUES (1, ?)",
            (stored,),
        )
    assert selection.load() is None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT project_id FROM packaged_product_selection WHERE slot = 1"
        ).fetchone()
    assert row is not None and row["project_id"] == stored


@pytest.mark.parametrize(
    "invalid",
    [None, True, b"product-existing", " ", "p\x00", "p\u202e", "p\ud800"],
)
def test_invalid_selection_never_overwrites_previous_selection(
    tmp_path: Path, invalid: object
) -> None:
    _store, selection = _selection(tmp_path)
    selection.select("product-existing")
    with pytest.raises(PackagedProductJourneyError, match="selected ProductProject id"):
        selection.select(invalid)  # type: ignore[arg-type]
    assert selection.load() == "product-existing"


def test_normal_selection_keeps_existing_whitespace_normalization(tmp_path: Path) -> None:
    _store, selection = _selection(tmp_path)
    selection.select("  product-existing  ")
    assert selection.load() == "product-existing"


@pytest.mark.parametrize(
    "command",
    ["Open ProductProjects", "Reopen ProductProjectManager", "Відкрий ProductProjectXYZ"],
)
def test_reopen_prefix_must_end_on_a_command_boundary(command: str) -> None:
    assert packaged_product_reopen_target(command) is None


def test_valid_colon_reopen_and_explicit_missing_id_are_preserved() -> None:
    project_id = "product-" + "a" * 64
    assert packaged_product_reopen_target("Open ProductProject:" + project_id) == project_id
    with pytest.raises(PackagedProductJourneyError, match="64 hex"):
        packaged_product_reopen_target("Open ProductProject")

def test_invalid_utf8_stored_as_sqlite_text_is_not_loaded_or_rewritten(
    tmp_path: Path,
) -> None:
    store, selection = _selection(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO packaged_product_selection(slot, project_id) "
            "VALUES (1, CAST(X'80' AS TEXT))"
        )
    assert selection.load() is None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT hex(CAST(project_id AS BLOB)) AS raw_id "
            "FROM packaged_product_selection WHERE slot = 1"
        ).fetchone()
    assert row is not None and row["raw_id"] == "80"
