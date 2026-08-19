from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    IngestDisposition,
    LocalCorpusService,
    LocalFileTooLargeError,
    LocalPathPolicyError,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
    normalize_text,
)


def _repo(tmp_path: Path) -> tuple[SQLiteStore, ResearchRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    return store, repository


def test_schema_migrates_to_research_version(tmp_path: Path) -> None:
    store, _ = _repo(tmp_path)
    assert store.schema_version() == 8


def test_normalization_is_stable_for_unicode_and_whitespace() -> None:
    assert normalize_text("Ａ\tБ\r\n\r\n\r\n  В  ") == "A Б\n\nВ"


def test_local_ingest_deduplicates_normalized_content_and_preserves_origins(
    tmp_path: Path,
) -> None:
    _, repository = _repo(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes("Український\tтекст\r\nпро гранти".encode("utf-8"))
    second.write_bytes("Український текст\nпро гранти\n".encode("utf-8"))
    service = LocalCorpusService(repository, allowed_root=root)

    one = SourceSpec("s1", "ws", SourceKind.LOCAL_FILE, str(first))
    two = SourceSpec("s2", "ws", SourceKind.LOCAL_FILE, str(second))
    result_one = service.ingest(one)
    result_two = service.ingest(two)

    assert result_one.disposition is IngestDisposition.CREATED
    assert result_two.disposition is IngestDisposition.DEDUPLICATED
    assert result_one.document.document_id == result_two.document.document_id
    assert repository.origin_count(result_one.document.document_id) == 2


def test_fts5_unicode_search_survives_repository_restart(tmp_path: Path) -> None:
    store, repository = _repo(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    source_path = root / "grant.txt"
    source_path.write_text("Освітній грант для українських студентів", encoding="utf-8")
    service = LocalCorpusService(repository, allowed_root=root)
    source = SourceSpec("grant-source", "ws", SourceKind.LOCAL_FILE, str(source_path))
    created = service.ingest(source)

    restarted = ResearchRepository(SQLiteStore(store.path))
    hits = restarted.search("ws", "українських студентів")

    assert [hit.document_id for hit in hits] == [created.document.document_id]
    assert "українських" in hits[0].snippet.casefold()


def test_html_json_and_csv_are_deterministically_extracted(tmp_path: Path) -> None:
    _, repository = _repo(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    (root / "page.html").write_text(
        "<h1>Grant</h1><script>secret()</script><p>Україна</p>", encoding="utf-8"
    )
    (root / "data.json").write_text('{"b": 2, "a": "тест"}', encoding="utf-8")
    (root / "table.csv").write_text("name,value\nГрант,10\n", encoding="utf-8")
    service = LocalCorpusService(repository, allowed_root=root)

    for index, name in enumerate(("page.html", "data.json", "table.csv"), start=1):
        source = SourceSpec(f"s{index}", "ws", SourceKind.LOCAL_FILE, str(root / name))
        assert service.ingest(source).disposition is IngestDisposition.CREATED

    assert repository.search("ws", "Україна")
    assert repository.search("ws", "тест")
    assert repository.search("ws", "Грант")
    assert not repository.search("ws", "secret")


def test_local_path_escape_fails_closed(tmp_path: Path) -> None:
    _, repository = _repo(tmp_path)
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    service = LocalCorpusService(repository, allowed_root=root)
    source = SourceSpec("outside", "ws", SourceKind.LOCAL_FILE, str(outside))

    with pytest.raises(LocalPathPolicyError):
        service.ingest(source)


def test_oversize_source_fails_before_read(tmp_path: Path) -> None:
    _, repository = _repo(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    large = root / "large.txt"
    large.write_text("0123456789", encoding="utf-8")
    service = LocalCorpusService(repository, allowed_root=root)
    source = SourceSpec("large", "ws", SourceKind.LOCAL_FILE, str(large))

    with pytest.raises(LocalFileTooLargeError):
        service.ingest(source, max_bytes=4)


def test_search_treats_fts_operators_as_literals(tmp_path: Path) -> None:
    _, repository = _repo(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    source_path = root / "literal.txt"
    source_path.write_text("alpha OR beta", encoding="utf-8")
    service = LocalCorpusService(repository, allowed_root=root)
    service.ingest(SourceSpec("literal", "ws", SourceKind.LOCAL_FILE, str(source_path)))

    assert repository.search("ws", "alpha OR beta")
