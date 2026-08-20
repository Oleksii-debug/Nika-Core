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

Query results use the existing canonical `research_result_sets` and `research_result_items` tables and remain retrievable after restart. For an unfiltered query, the result preserves all recorded origins for each matched deduplicated document.

For source-ID, source-kind or freshness filtered queries, persisted evidence is scoped to the same origin policy that admitted the hit. This prevents a document that is deduplicated across several origins from leaking excluded provenance merely because one allowed origin matched. Scoped evidence is prepared before persistence, then the result-set header and all result items commit in one SQLite transaction; there is no committed broad-evidence state followed by a later narrowing update.

The query execution wrapper retains the requested mode/filter specification for immediate deterministic rendering. Versioned reusable profile/source-set definitions are persisted separately through canonical migration 12 and reuse this same query contract.

The text renderer exposes query mode, active filters, result count, title/snippet and source provenance as ordinary text suitable for screen-reader consumption. It does not award `HUMAN_TESTED` or `NVDA_VERIFIED`.

## REUSE / ADAPT / CUSTOM

- REUSE: canonical SQLite database and SQLite FTS5 `unicode61` corpus index.
- REUSE: existing corpus identity/dedup/provenance and persisted research result-set tables.
- ADAPT: the result persistence path scopes evidence atomically when origin filters are active.
- CUSTOM (thin): Nika-owned query specification, safe FTS construction, workspace/source policy filters and accessible text projection.

No new dependency is introduced by this batch.

## Failure policy

The API fails closed for an empty workspace/query, limits outside 1..100, empty source/media filter values, unknown source IDs, cross-workspace source IDs, and freshness filters that explicitly exclude HTTP sources. SQL values are parameterized; only placeholder counts and fixed query fragments are constructed by code.

## Versioned profile extension

`ResearchProfile` and `ResearchSourceSet` definitions pin reusable source membership and deterministic query behavior by version. They reference this query implementation rather than creating a second search engine, scheduler, database or LLM path. See `docs/UNIVERSAL_RESEARCH_PROFILE_CONTRACT.md`.
