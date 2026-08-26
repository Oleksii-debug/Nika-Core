# Table Tennis Stats Collector

Status: TT01 backend workspace proof. This document does not claim packaged UI, HUMAN_TESTED, or NVDA_VERIFIED evidence.

## Purpose

Table Tennis Stats Collector is a thin domain workspace over Nika Core's existing research and durable-data boundaries. It accepts already normalized completed-match observations, keeps an append-only revision history in SQLite, derives deterministic player statistics from the current revision of each source record, and emits simple UTF-8 text/CSV artifacts.

It intentionally does **not** implement a crawler, browser scraper, HTTP client, OCR stack, model provider, or generic report framework. Acquisition remains the responsibility of Universal Research and its permitted source adapters. This preserves the repository rule `REUSE -> ADAPT -> CUSTOM (thin)` and avoids a second research stack.

## Scope and ownership

TT01 owns only:

- `src/nika_core/table_tennis/**`
- `tests/test_table_tennis_*.py`
- `docs/TABLE_TENNIS_STATS_COLLECTOR.md`

It does not modify Universal Research, ModelGateway, Experiments, shared UI, Product Factory, shared migrations, dependencies, or GitHub workflows.

## Input contract

`MatchObservation` is the normalized boundary. A caller supplies:

- stable `source_id` and `source_record_id` provenance identity;
- monotonically increasing integer `source_revision` beginning at `1`;
- a non-secret `source_locator` for provenance/audit navigation;
- `source_evidence_sha256`, binding the normalized row to upstream evidence bytes;
- timezone-aware `observed_at` and `played_at` timestamps;
- event/round labels;
- two stable player IDs plus display names;
- completed match set totals with a single winner.

Strings are NFC-normalized and control separators are rejected. Naive timestamps, boolean scores, negative scores, tied completed matches, a player against itself, and observation timestamps earlier than match time fail before persistence.

`source_locator` must identify public/non-secret source evidence. Callers must not place cookies, bearer tokens, signed private URLs, browser profiles, credentials, or other secrets in this field.

## Durable revision semantics

The repository uses `SQLiteStore.connection()` but owns separate `table_tennis_*` tables and its own migration ledger, so TT01 does not edit the shared schema registry.

For every logical `(source_id, source_record_id)`:

1. the first accepted revision must be `1`;
2. exact replay of the current revision and identical canonical payload is idempotent (`REPLAYED`);
3. the same revision with different content is rejected as mutation;
4. rollback or skipped revisions are rejected;
5. the next contiguous revision is append-only and atomically becomes the new head (`REVISED`);
6. historical revisions remain stored but never double-count current statistics.

Writes use `BEGIN IMMEDIATE` with a bounded SQLite busy timeout. This gives one durable head under concurrent same-record ingestion. Initialization is also serialized so concurrent first-use migrations converge.

Every normalized payload has its own SHA-256 digest, distinct from the required upstream `source_evidence_sha256`. Reads reconstruct the current observation and recompute the normalized digest. A missing head target, row tamper, or head/hash mismatch fails closed with `TableTennisIntegrityError`; corrupted state is not silently converted into statistics.

## Deterministic statistics

`TableTennisStatsService.snapshot()` aggregates only current heads. For each player it returns:

- matches, wins, losses;
- sets for and against;
- set difference;
- integer `win_rate_millionths` (no binary floating-point dependency).

Aggregation identity is the stable player ID, not display-name text. If a player's display name changes, the deterministic name from the latest match/revision tuple is used for presentation. Snapshot order is stable by player ID and does not depend on ingestion order.

`ingest_many()` deliberately persists one observation at a time in canonical source/record/revision order. If a later observation fails, a valid prefix can be replayed safely after repair; exact replay prevents duplicate effects.

## Accessible artifacts

The thin report layer returns bytes; it never writes arbitrary caller-controlled filesystem paths.

- `render_text_report()` returns UTF-8 plain text with explicit textual headings, counts, column names, and one record per line.
- `render_csv_report()` returns UTF-8 CSV with fixed columns. Player IDs/names beginning with spreadsheet formula characters are apostrophe-prefixed to prevent formula execution when opened in spreadsheet software.

These are machine-readable/text-accessible backend artifacts. They are not evidence of an NVDA-tested packaged UI. A future workspace UI should consume these contracts through the shared Nika UI/report boundary rather than adding bespoke inaccessible controls.

## Universal Research handoff

The intended flow is:

`permitted source -> Universal Research/source adapter -> deterministic parser -> MatchObservation -> TableTennisStatsService -> text/CSV artifact`

TT01 begins at `MatchObservation`. This is deliberate: network policy, robots/source terms, retries, evidence capture, and browser/OCR fallback belong to Universal Research. A later integration may add a narrow adapter once the active Universal Research lane stabilizes, but that adapter must map existing evidence to this contract and must not add a custom crawler.

## Failure and restart behavior

- Exact duplicate after crash: replay returns `REPLAYED`; no second row is added.
- Crash after an accepted transaction: restart reads the durable head and current stats.
- Correction after restart: next contiguous revision appends and supersedes the prior result.
- Conflicting same revision: reject; upstream must issue a new revision.
- Revision gap/rollback: reject; caller must resolve missing/stale source authority.
- SQLite tamper or missing authority row: fail closed.
- Unicode and paths containing spaces are supported by `SQLiteStore` and covered by tests.

## Verification boundary

TT01 unit coverage includes contract rejection, exact replay, conflicting revisions, gap rejection, revision supersession, restart recovery, deterministic aggregation, latest display-name selection, Unicode/space database paths, concurrent replay convergence, row/hash tamper detection, explicit text output, UTF-8 CSV, and spreadsheet formula-injection protection.

Repo-wide Linux/Windows CI at the exact branch SHA remains the acceptance authority after a PR is opened. `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` until real human/NVDA evidence exists.
