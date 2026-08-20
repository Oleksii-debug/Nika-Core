from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    DeterministicResearchQueryService,
    ExtractedDocument,
    FreshnessState,
    NetworkResearchRepository,
    ResearchProfile,
    ResearchProfileRepository,
    ResearchProfileService,
    ResearchRepository,
    ResearchSearchFilters,
    ResearchSourceRef,
    ResearchSourceSet,
    ResearchWorkspace,
    SearchMode,
    SourceKind,
    SourceSpec,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _seed_local_sources(store: SQLiteStore) -> ResearchRepository:
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    for source_id, locator in (
        ("local-a", "C:/Corpus/A.txt"),
        ("local-b", "C:/Corpus/B.txt"),
    ):
        repository.upsert_source(
            SourceSpec(
                source_id=source_id,
                workspace_id="ws",
                kind=SourceKind.LOCAL_FILE,
                locator=locator,
            )
        )
    return repository


def _profile_stack(store: SQLiteStore) -> tuple[ResearchProfileRepository, ResearchProfileService]:
    network = NetworkResearchRepository(store)
    definitions = ResearchProfileRepository(store)
    queries = DeterministicResearchQueryService(store=store, network_repository=network)
    return definitions, ResearchProfileService(repository=definitions, query_service=queries)


def test_sqlite_store_upgrades_existing_schema_11_to_profile_migration_12(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 12
    path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(1, 12):
        for statement in MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, f"v{version}"),
        )
    conn.execute(
        "INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at) "
        "VALUES ('existing', 'Existing', 'before', 'before')"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.initialize()

    assert store.schema_version() == SCHEMA_VERSION
    with store.connection() as upgraded:
        assert upgraded.execute(
            "SELECT name FROM research_workspaces WHERE workspace_id='existing'"
        ).fetchone()["name"] == "Existing"
        tables = {
            row["name"]
            for row in upgraded.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'research_%'"
            ).fetchall()
        }
    assert {"research_source_sets", "research_source_set_members", "research_profiles"} <= tables


def test_source_set_and_profile_versions_are_immutable_and_restart_safe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_local_sources(store)
    definitions, _ = _profile_stack(store)

    source_set_v1 = ResearchSourceSet(
        "grants",
        "ws",
        1,
        "Grant sources",
        (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
    )
    source_set_v2 = ResearchSourceSet(
        "grants",
        "ws",
        2,
        "Grant sources",
        (
            ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),
            ResearchSourceRef("local-b", SourceKind.LOCAL_FILE),
        ),
    )
    assert definitions.save_source_set(source_set_v1) == source_set_v1
    assert definitions.save_source_set(source_set_v1) == source_set_v1
    definitions.save_source_set(source_set_v2)
    with pytest.raises(ValueError, match="different content"):
        definitions.save_source_set(
            ResearchSourceSet(
                "grants",
                "ws",
                1,
                "Mutated",
                (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
            )
        )

    profile_v1 = ResearchProfile(
        "education-grants",
        "ws",
        1,
        "Education grants",
        "grants",
        1,
        "освіта грант",
        SearchMode.LITERAL,
        ResearchSearchFilters(media_types=("text/plain",)),
        25,
    )
    profile_v2 = ResearchProfile(
        "education-grants",
        "ws",
        2,
        "Education grants phrase",
        "grants",
        2,
        "освітній грант",
        SearchMode.PHRASE,
        ResearchSearchFilters(),
        10,
    )
    assert definitions.save_profile(profile_v1) == profile_v1
    assert definitions.save_profile(profile_v1) == profile_v1
    definitions.save_profile(profile_v2)
    with pytest.raises(ValueError, match="different content"):
        definitions.save_profile(
            ResearchProfile(
                "education-grants",
                "ws",
                1,
                "Changed",
                "grants",
                1,
                "освіта грант",
            )
        )

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    loaded = ResearchProfileRepository(restarted)
    assert restarted.schema_version() == SCHEMA_VERSION
    assert loaded.load_source_set("grants", 1) == source_set_v1
    assert loaded.load_source_set("grants") == source_set_v2
    assert loaded.load_profile("education-grants", 1) == profile_v1
    assert loaded.load_profile("education-grants") == profile_v2


def test_profile_execution_scopes_persisted_provenance_after_exact_dedup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = _seed_local_sources(store)
    shared_text = "Український освітній грант для студентів та викладачів."
    first = repository.ingest_document(
        SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/A.txt"),
        ExtractedDocument("Grant A", shared_text, "text/plain"),
    )
    second = repository.ingest_document(
        SourceSpec("local-b", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/B.txt"),
        ExtractedDocument("Grant B", shared_text, "text/plain"),
    )
    assert first.document.document_id == second.document.document_id
    assert repository.origin_count(first.document.document_id) == 2

    definitions, profiles = _profile_stack(store)
    definitions.save_source_set(
        ResearchSourceSet(
            "only-a",
            "ws",
            1,
            "Only A",
            (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
        )
    )
    definitions.save_profile(
        ResearchProfile("grants-a", "ws", 1, "A grants", "only-a", 1, "освітній грант")
    )

    execution = profiles.execute("grants-a")
    assert len(execution.query.result_set.items) == 1
    assert {e.source_id for e in execution.query.result_set.items[0].evidence} == {"local-a"}
    persisted = NetworkResearchRepository(store).get_result_set(
        execution.query.result_set.result_set_id
    )
    assert {e.source_id for e in persisted.items[0].evidence} == {"local-a"}

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    restarted_definitions, restarted_profiles = _profile_stack(restarted)
    assert restarted_definitions.load_profile("grants-a").source_set_version == 1
    again = restarted_profiles.execute("grants-a")
    assert {e.source_id for e in again.query.result_set.items[0].evidence} == {"local-a"}


def test_source_set_validation_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = _seed_local_sources(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="other", name="Other"))
    NetworkResearchRepository(store).register_source(
        SourceSpec("web", "ws", SourceKind.HTTP, "https://example.org/research")
    )
    definitions = ResearchProfileRepository(store)

    with pytest.raises(ValueError, match="duplicate"):
        definitions.save_source_set(
            ResearchSourceSet(
                "dup",
                "ws",
                1,
                "Duplicate",
                (
                    ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),
                    ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),
                ),
            )
        )
    with pytest.raises(ValueError, match="kind mismatch"):
        definitions.save_source_set(
            ResearchSourceSet(
                "wrong-kind",
                "ws",
                1,
                "Wrong kind",
                (ResearchSourceRef("web", SourceKind.LOCAL_FILE),),
            )
        )
    with pytest.raises(ValueError, match="workspace"):
        definitions.save_source_set(
            ResearchSourceSet(
                "cross-workspace",
                "other",
                1,
                "Cross workspace",
                (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
            )
        )


def test_profile_rejects_unpinned_source_ids_and_local_only_freshness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_local_sources(store)
    definitions = ResearchProfileRepository(store)
    definitions.save_source_set(
        ResearchSourceSet(
            "local-only",
            "ws",
            1,
            "Local only",
            (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
        )
    )

    with pytest.raises(ValueError, match="source IDs come from"):
        definitions.save_profile(
            ResearchProfile(
                "bad-source-filter",
                "ws",
                1,
                "Bad",
                "local-only",
                1,
                "grant",
                filters=ResearchSearchFilters(source_ids=("local-b",)),
            )
        )
    with pytest.raises(ValueError, match="freshness filters require an HTTP source"):
        definitions.save_profile(
            ResearchProfile(
                "bad-freshness",
                "ws",
                1,
                "Bad freshness",
                "local-only",
                1,
                "grant",
                filters=ResearchSearchFilters(freshness=(FreshnessState.CURRENT,)),
            )
        )
