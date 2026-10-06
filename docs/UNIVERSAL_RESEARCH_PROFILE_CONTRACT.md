# Universal Research profile and source-set contract

Status: DEV01 implementation contract. This document does not claim Product Journey, packaged UI, HUMAN_TESTED or NVDA_VERIFIED completion.

## Purpose

Universal Research uses reusable versioned definitions instead of separate crawlers for grants, products, education, events or later opportunity domains. A `ResearchProfile` describes deterministic query behavior; a `ResearchSourceSet` pins exactly which registered sources that profile version may search.

The implementation reuses the canonical Nika SQLite database and the existing deterministic FTS5 query/result-set path. It does not create another scheduler, database, runtime kernel, ModelGateway, browser engine, OCR engine or vector store.

## Versioning and identity

`ResearchSourceSet` identity is `(source_set_id, version)`. A version contains an ordered non-empty list of unique `(source_id, source_kind)` references. Every referenced source must already exist in the same research workspace and its declared kind must match the registered source kind.

`ResearchProfile` identity is `(profile_id, version)`. A profile pins one exact `(source_set_id, source_set_version)` plus query text, query mode, deterministic filters and result limit. A stored version is immutable: saving the exact same definition is idempotent, while trying to reuse the same identity for different content fails closed. New behavior requires a new positive version.

Profiles do not embed URLs, local file paths, authentication material, cookies, tokens or other secrets. Source configuration remains in the existing source registries. A profile cannot inject arbitrary source IDs through query filters; source membership comes only from its pinned source-set version.

## Persistence and migration

Schema migration 12 appends three tables to the canonical ordered SQLite architecture:

- `research_source_sets`;
- `research_source_set_members`;
- `research_profiles`.

Existing schema-11 databases upgrade in place without replacing the existing research/corpus/network tables. Restart reloads the same immutable source-set/profile versions. A database newer than the running application continues to fail closed through the existing `SQLiteStore` policy.

## Deterministic execution

`ResearchProfileService.execute()` loads the requested profile version, loads its exact pinned source-set version, and constructs the already-established `ResearchQuerySpec`. Search remains SQLite FTS5 with explicit literal/phrase behavior and deterministic media/source-kind/freshness filters. No LLM is needed.

The source-set source IDs are injected by the service and validated again by the deterministic query layer. This keeps direct query callers and profile callers on one search implementation rather than creating a second engine.

## Provenance and privacy invariant

A corpus document can be content-deduplicated across multiple origins. Therefore a result that matched an allowed source must not automatically reveal every other origin of the same document.

Filtered query result persistence scopes evidence to the same source-ID/source-kind/freshness policy that admitted the hit. Evidence is prepared before persistence and the result-set header plus items are committed in one SQLite transaction. There is no intermediate committed state containing broader provenance followed by a narrowing update.

Unfiltered queries retain all recorded provenance. Media-type filtering constrains the document, not its origin, so it does not remove provenance by itself.

## REUSE / ADAPT / CUSTOM

- **REUSE:** Python `sqlite3`, existing canonical `SQLiteStore`, existing ordered migration mechanism, existing research source registries, FTS5 corpus index and result-set schema.
- **ADAPT:** existing deterministic query service now persists filtered provenance atomically.
- **CUSTOM (thin):** immutable Nika `ResearchSourceSet`/`ResearchProfile` contracts, source membership/workspace validation, version pinning and profile execution glue.

No new dependency is introduced.

## Explicit non-scope

This batch does not add HTTP fetching, Playwright, OCR, vector retrieval, LLM analysis, scheduling, review cards, UI wiring or report-file export. Those capabilities must reuse their existing/shared lanes and ports when added. OCR_NEEDED remains a DEV05 boundary rather than profile logic.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`.
