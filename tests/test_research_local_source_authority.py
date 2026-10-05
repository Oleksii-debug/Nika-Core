from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.repository import ResearchRepository


def _repository(tmp_path: Path) -> tuple[SQLiteStore, ResearchRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("owner-a", "First"))
    repository.upsert_workspace(ResearchWorkspace("owner-b", "Second"))
    return store, repository


@pytest.mark.parametrize("new_locator", ["/shared/item.txt", "/changed/item.txt"])
def test_local_source_id_cannot_move_to_another_workspace(
    tmp_path: Path, new_locator: str
) -> None:
    store, repository = _repository(tmp_path)
    original = SourceSpec("shared-id", "owner-a", SourceKind.LOCAL_FILE, "/shared/item.txt")
    repository.upsert_source(original)

    with pytest.raises(ValueError, match="another workspace or source kind"):
        repository.upsert_source(
            SourceSpec("shared-id", "owner-b", SourceKind.LOCAL_FILE, new_locator)
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT workspace_id, kind, locator FROM research_sources WHERE source_id=?",
            ("shared-id",),
        ).fetchone()
        assert tuple(row) == ("owner-a", SourceKind.LOCAL_FILE.value, original.locator)
        assert conn.execute(
            "SELECT COUNT(*) FROM research_sources WHERE workspace_id='owner-b'"
        ).fetchone()[0] == 0

    # A rejected collision does not poison legitimate idempotent re-registration.
    repository.upsert_source(original)


def test_local_source_re_registration_inside_original_workspace_is_supported(
    tmp_path: Path,
) -> None:
    store, repository = _repository(tmp_path)
    old = SourceSpec("same-id", "owner-a", SourceKind.LOCAL_FILE, "/old.txt")
    new = SourceSpec("same-id", "owner-a", SourceKind.LOCAL_FILE, "/new.txt")
    repository.upsert_source(old)
    repository.upsert_source(old)
    repository.upsert_source(new)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT workspace_id, kind, locator FROM research_sources WHERE source_id=?",
            ("same-id",),
        ).fetchone()
    assert tuple(row) == ("owner-a", "local_file", "/new.txt")


def test_local_source_id_cannot_relabel_existing_http_source(tmp_path: Path) -> None:
    store, repository = _repository(tmp_path)
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            ("source-id", "owner-a", "http", "https://example.test/source", "now", "now"),
        )

    with pytest.raises(ValueError, match="another workspace or source kind"):
        repository.upsert_source(
            SourceSpec("source-id", "owner-a", SourceKind.LOCAL_FILE, "/local.txt")
        )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT workspace_id, kind, locator FROM research_sources WHERE source_id=?",
            ("source-id",),
        ).fetchone()
    assert tuple(row) == ("owner-a", "http", "https://example.test/source")


@pytest.mark.parametrize(
    "source",
    [
        SourceSpec("", "owner-a", SourceKind.LOCAL_FILE, "/file.txt"),
        SourceSpec("  ", "owner-a", SourceKind.LOCAL_FILE, "/file.txt"),
        SourceSpec("id", "", SourceKind.LOCAL_FILE, "/file.txt"),
        SourceSpec("id", "owner-a", SourceKind.LOCAL_FILE, "  "),
        SourceSpec(1, "owner-a", SourceKind.LOCAL_FILE, "/file.txt"),
        SourceSpec("id", b"owner-a", SourceKind.LOCAL_FILE, "/file.txt"),
        SourceSpec("id", "owner-a", SourceKind.LOCAL_FILE, b"/file.txt"),
    ],
)
def test_local_source_rejects_invalid_identity_before_sql(
    tmp_path: Path, source: SourceSpec
) -> None:
    store, repository = _repository(tmp_path)
    with pytest.raises(ValueError, match="must be nonempty text"):
        repository.upsert_source(source)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 0


def test_local_source_rejects_forged_kind_without_sql_mutation(tmp_path: Path) -> None:
    store, repository = _repository(tmp_path)
    forged = SourceSpec("id", "owner-a", "local_file", "/file.txt")
    with pytest.raises(ValueError, match="canonical local_file"):
        repository.upsert_source(forged)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 0
