from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.repository import ResearchRepository

BAD_UNICODE = chr(0xD800)


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
    network = NetworkResearchRepository(store)
    network.register_source(
        SourceSpec(
            "source-id",
            "owner-a",
            SourceKind.HTTP,
            "https://example.test/source",
        )
    )

    with pytest.raises(ValueError, match="another workspace or source kind"):
        repository.upsert_source(
            SourceSpec("source-id", "owner-a", SourceKind.LOCAL_FILE, "/local.txt")
        )

    http_source = network.get_source("source-id")
    assert http_source.workspace_id == "owner-a"
    assert http_source.url == "https://example.test/source"
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM research_sources WHERE source_id=?",
            ("source-id",),
        ).fetchone()[0] == 0


def test_local_source_claim_reserves_writer_before_cross_table_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository = _repository(tmp_path)
    statements: list[str] = []
    original_connection = store.connection

    @contextmanager
    def traced_connection():
        with original_connection() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(store, "connection", traced_connection)
    repository.upsert_source(
        SourceSpec("serialized-id", "owner-a", SourceKind.LOCAL_FILE, "/local.txt")
    )

    normalized = [statement.strip().upper() for statement in statements]
    begin_index = normalized.index("BEGIN IMMEDIATE")
    collision_index = next(
        index
        for index, statement in enumerate(normalized)
        if "SELECT 1 FROM RESEARCH_HTTP_SOURCES WHERE SOURCE_ID=" in statement
    )
    insert_index = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("INSERT INTO RESEARCH_SOURCES")
    )
    assert begin_index < collision_index < insert_index


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



@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source_id", BAD_UNICODE),
        ("workspace_id", BAD_UNICODE),
        ("locator", BAD_UNICODE),
    ],
)
def test_local_source_rejects_unencodable_identity_before_sql(
    tmp_path: Path,
    field_name: str,
    value: str,
) -> None:
    store, repository = _repository(tmp_path)
    values = {
        "source_id": "id",
        "workspace_id": "owner-a",
        "locator": "/valid/файл.txt",
    }
    values[field_name] = value
    source = SourceSpec(
        values["source_id"],
        values["workspace_id"],
        SourceKind.LOCAL_FILE,
        values["locator"],
    )

    with pytest.raises(ValueError, match="valid UTF-8"):
        repository.upsert_source(source)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 0


@pytest.mark.parametrize(
    "locator",
    [
        "/bad\x00name.txt",
        "/bad\nname.txt",
        "/bad\tname.txt",
        "/bad" + chr(0x7F) + "name.txt",
    ],
)
def test_local_source_rejects_control_characters_in_locator_before_sql(
    tmp_path: Path,
    locator: str,
) -> None:
    store, repository = _repository(tmp_path)
    source = SourceSpec("id", "owner-a", SourceKind.LOCAL_FILE, locator)

    with pytest.raises(ValueError, match="control characters"):
        repository.upsert_source(source)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 0


def test_local_source_preserves_valid_unicode_locator(tmp_path: Path) -> None:
    store, repository = _repository(tmp_path)
    source = SourceSpec(
        "джерело",
        "owner-a",
        SourceKind.LOCAL_FILE,
        "/дані/партія №1.pgn",
    )

    repository.upsert_source(source)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT source_id, workspace_id, locator FROM research_sources WHERE source_id=?",
            (source.source_id,),
        ).fetchone()
    assert tuple(row) == (source.source_id, source.workspace_id, source.locator)
