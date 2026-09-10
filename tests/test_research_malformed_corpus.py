from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    ExtractionStatus,
    LocalCorpusService,
    LocalIngestionError,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)


def _setup(tmp_path: Path) -> tuple[SQLiteStore, ResearchRepository, LocalCorpusService, Path]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    root = tmp_path / "sources"
    root.mkdir()
    return store, repository, LocalCorpusService(repository, allowed_root=root), root


def _count(store: SQLiteStore, table: str) -> int:
    with store.connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def _assert_retrieval_empty(store: SQLiteStore) -> None:
    assert _count(store, "corpus_documents") == 0
    assert _count(store, "corpus_chunks") == 0
    assert _count(store, "corpus_fts") == 0
    assert _count(store, "corpus_origins") == 0


def test_empty_file_is_recorded_as_empty_artifact_but_not_published(tmp_path: Path) -> None:
    store, _, service, root = _setup(tmp_path)
    source_path = root / "empty.txt"
    source_path.write_bytes(b"")
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")

    result = service.ingest_artifact(
        SourceSpec("empty", "ws", SourceKind.LOCAL_FILE, str(source_path)),
        blob_store=blob_store,
    )

    assert result.extraction.status is ExtractionStatus.EMPTY
    assert result.extraction.normalized_text_sha256 is None
    assert result.corpus is None
    _assert_retrieval_empty(store)


def test_invalid_encoding_fails_without_retrieval_publication(tmp_path: Path) -> None:
    store, _, service, root = _setup(tmp_path)
    source_path = root / "invalid.txt"
    source_path.write_bytes(b"valid-prefix\xfffabricated-suffix")
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")

    with pytest.raises(LocalIngestionError, match="expected UTF-8"):
        service.ingest_artifact(
            SourceSpec("invalid", "ws", SourceKind.LOCAL_FILE, str(source_path)),
            blob_store=blob_store,
        )

    _assert_retrieval_empty(store)
    with store.connection() as conn:
        row = conn.execute("SELECT status FROM corpus_extractions").fetchone()
    assert row["status"] == ExtractionStatus.FAILED.value


def test_huge_logical_line_fails_at_explicit_bound(tmp_path: Path) -> None:
    store, _, service, root = _setup(tmp_path)
    source_path = root / "huge-line.txt"
    source_path.write_text("123456789", encoding="utf-8")

    with pytest.raises(LocalIngestionError, match="logical line exceeds 8 characters"):
        service.ingest(
            SourceSpec("huge-line", "ws", SourceKind.LOCAL_FILE, str(source_path)),
            max_logical_line_chars=8,
        )

    _assert_retrieval_empty(store)
    assert _count(store, "research_sources") == 0


@pytest.mark.parametrize(
    "source",
    (
        SourceSpec("", "ws", SourceKind.LOCAL_FILE, "placeholder"),
        SourceSpec("bad", "", SourceKind.LOCAL_FILE, "placeholder"),
        SourceSpec("bad", "ws", SourceKind.LOCAL_FILE, ""),
        SourceSpec("bad", "ws", SourceKind.HTTP, "placeholder"),
    ),
)
def test_bad_source_metadata_fails_before_file_access(
    tmp_path: Path,
    source: SourceSpec,
) -> None:
    store, _, service, _ = _setup(tmp_path)

    with pytest.raises(ValueError):
        service.ingest(source)

    _assert_retrieval_empty(store)
    assert _count(store, "research_sources") == 0


def test_conflicting_duplicate_source_id_cannot_rebind_provenance(tmp_path: Path) -> None:
    store, repository, service, root = _setup(tmp_path)
    first_path = root / "first.txt"
    second_path = root / "second.txt"
    first_path.write_text("trusted alpha evidence", encoding="utf-8")
    second_path.write_text("attacker beta evidence", encoding="utf-8")
    first = SourceSpec("duplicate", "ws", SourceKind.LOCAL_FILE, str(first_path))
    second = SourceSpec("duplicate", "ws", SourceKind.LOCAL_FILE, str(second_path))

    created = service.ingest(first)
    with pytest.raises(ValueError, match="already bound"):
        service.ingest(second)

    assert [hit.document_id for hit in repository.search("ws", "alpha")] == [
        created.document.document_id
    ]
    assert repository.search("ws", "beta") == []
    with store.connection() as conn:
        row = conn.execute(
            "SELECT locator FROM research_sources WHERE source_id='duplicate'"
        ).fetchone()
    assert row["locator"] == str(first_path.resolve())
    assert _count(store, "corpus_documents") == 1
    assert _count(store, "corpus_origins") == 1


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    (
        ("truncated.json", b'{"items": [1, 2', "malformed JSON"),
        ("truncated.csv", b'name,value\n"unfinished', "malformed CSV"),
    ),
)
def test_truncated_structured_content_fails_without_partial_retrieval(
    tmp_path: Path,
    name: str,
    payload: bytes,
    message: str,
) -> None:
    store, _, service, root = _setup(tmp_path)
    source_path = root / name
    source_path.write_bytes(payload)

    with pytest.raises(LocalIngestionError, match=message):
        service.ingest(SourceSpec(name, "ws", SourceKind.LOCAL_FILE, str(source_path)))

    _assert_retrieval_empty(store)
    assert _count(store, "research_sources") == 0
