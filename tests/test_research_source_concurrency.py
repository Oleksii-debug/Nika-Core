from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    NetworkResearchRepository,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.source_identity import ResearchSourceIdentityError


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(ResearchWorkspace("ws", "Research"))
    return store


def _concurrent_register(
    store: SQLiteStore,
    first: SourceSpec,
    second: SourceSpec,
) -> tuple[tuple[str, str], tuple[str, str]]:
    barrier = Barrier(2)

    def run(source: SourceSpec) -> tuple[str, str]:
        repository = NetworkResearchRepository(SQLiteStore(store.path))
        barrier.wait()
        try:
            state = repository.register_source(source)
        except ResearchSourceIdentityError as exc:
            return ("error", exc.code)
        return ("ok", state.source_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(run, first), pool.submit(run, second))
        return (futures[0].result(), futures[1].result())


def test_concurrent_duplicate_locators_have_one_canonical_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    results = _concurrent_register(
        store,
        SourceSpec("source-a", "ws", SourceKind.HTTP, "https://example.com/source#one"),
        SourceSpec("source-b", "ws", SourceKind.HTTP, "HTTPS://EXAMPLE.COM:443/source#two"),
    )

    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert [value for status, value in results if status == "error"] == ["source_duplicate"]
    sources = NetworkResearchRepository(SQLiteStore(store.path)).list_sources("ws")
    assert len(sources) == 1
    assert sources[0].url == "https://example.com/source"


def test_concurrent_source_id_mutation_is_rejected_after_first_writer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    results = _concurrent_register(
        store,
        SourceSpec("shared", "ws", SourceKind.HTTP, "https://example.com/a"),
        SourceSpec("shared", "ws", SourceKind.HTTP, "https://example.com/b"),
    )

    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert [value for status, value in results if status == "error"] == [
        "source_locator_conflict"
    ]
    source = NetworkResearchRepository(SQLiteStore(store.path)).get_source("shared")
    assert source.url in {"https://example.com/a", "https://example.com/b"}
