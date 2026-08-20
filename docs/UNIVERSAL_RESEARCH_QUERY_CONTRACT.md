# Universal Research deterministic query contract

Status: DEV01 implementation contract. This document does not claim Product Journey or release completion.

## Scope

The deterministic query layer operates over the canonical SQLite corpus/FTS5 state. It does not invoke ModelGateway, an LLM, embeddings, a vector database, browser automation, OCR, or a second persistence kernel.

Supported query modes:
- `literal`: normalized whitespace-separated terms are individually quoted and combined with FTS5 `AND`; caller-provided FTS operators are never interpreted as syntax.
- `phrase`: the complete normalized query is passed as one quoted FTS5 phrase.

Supported deterministic filters:
- exact source IDs, validated to belong to the requested workspace;
- source kind (`local_file` or `http`);
- exact stored media type;
- HTTP freshness state.

Source ID, source kind and freshness predicates are evaluated against the same origin. A document cannot satisfy a requested source ID through one origin and a requested freshness state through another origin.

## Provenance and persistence

Query execution reuses `NetworkResearchRepository.save_result_set()`. Results therefore preserve the existing evidence snapshot and remain retrievable after process restart. The query execution wrapper retains the requested mode/filter specification for immediate deterministic rendering; persisted reusable profile/source-set definitions are a separate follow-on batch and require an ordered canonical SQLite migration rather than an ad-hoc store.

The text renderer exposes query mode, active filters, result count, title/snippet and source provenance as ordinary text suitable for screen-reader consumption. It does not award `HUMAN_TESTED` or `NVDA_VERIFIED`.

## REUSE / ADAPT / CUSTOM

- REUSE: canonical SQLite database and SQLite FTS5 `unicode61` corpus index.
- REUSE: existing corpus identity/dedup/provenance and persisted research result-set tables.
- ADAPT: none.
- CUSTOM (thin): Nika-owned query specification, safe FTS construction, workspace/source policy filters and accessible text projection.

No new dependency is introduced by this batch.

## Failure policy

The API fails closed for an empty workspace/query, limits outside 1..100, empty source/media filter values, unknown source IDs, cross-workspace source IDs, and freshness filters that explicitly exclude HTTP sources. SQL values are parameterized; only placeholder counts and fixed query fragments are constructed by code.

## Next compatible extension

The next persistence batch may add versioned `ResearchProfile` and `SourceSet` definitions through an ordered canonical SQLite migration. Those definitions should reference this query contract rather than creating a second search engine, scheduler, database or LLM path.
