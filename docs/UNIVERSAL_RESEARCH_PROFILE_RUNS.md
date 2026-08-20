# Universal Research durable profile runs

Status: DEV01 implementation contract. This document does not claim packaged UI, HUMAN_TESTED or NVDA_VERIFIED completion.

## Capability

`ResearchProfileRunService` turns one immutable `ResearchProfile` version into a durable canonical Nika TaskQueue job. Job creation resolves the requested/latest profile once and persists the exact profile version, source-set version and HTTP source membership in the task payload. Later profile edits therefore cannot silently change an already-created run.

A run reuses the existing incremental HTTP research engine for each pinned HTTP source, checkpointing source progress after every refresh. Local-file sources are not re-fetched by this service; their already-ingested corpus content remains queryable through the same pinned source set. After refresh, the service executes the existing deterministic SQLite FTS5 query path and persists the result set.

No second scheduler, database, runtime kernel, LLM gateway, browser engine, OCR engine or vector store is introduced.

## Restart and idempotency

The task checkpoint persists HTTP progress counters and the completed result-set ID. After pause/process restart, completed refresh indices are skipped rather than re-fetching them.

The final deterministic result-set identity is derived from the canonical TaskQueue task ID. The scoped result writer accepts an explicit stable result-set ID and returns an already-persisted matching result on replay. Therefore a crash after result persistence but before the final checkpoint/task transition cannot create a second result set for the same profile-run task.

An explicit result-set ID is accepted only for the same workspace/query identity. Reusing it for different query identity fails closed.

## Failure and last-good behavior

HTTP refresh dispositions are counted as changed, unchanged or failed using the same classification family as the existing refresh service. A failed refresh does not delete prior corpus content; the deterministic query still searches the currently persisted last-good corpus evidence permitted by the pinned profile/source-set filters. The summary exposes failed refresh count instead of silently converting failure to freshness success.

## Accessible report-model foundation

`ResearchProfileRunSummary` is a framework-neutral text-friendly record containing task state, exact profile/source-set versions, HTTP progress, changed/unchanged/failed counts, result-set ID and result count. `render_text()` projects those fields as ordinary text suitable for later accessible report/UI adapters without coupling this DEV01 lane to shared desktop UI ownership.

## REUSE / ADAPT / CUSTOM

- **REUSE:** canonical `TaskQueue`, `CheckpointService`, SQLite persistence, immutable profile/source-set repository, incremental HTTP refresh service boundary, deterministic FTS5 query service and scoped provenance/result tables.
- **ADAPT:** deterministic result persistence accepts an optional stable caller-owned result ID so crash replay can be exactly-once at the profile-run boundary.
- **CUSTOM (thin):** profile-run orchestration, exact version pinning, progress summary and text report projection.

No dependency or schema migration is added by this batch.

## Explicit non-scope

This batch does not add scheduler policy, cron UI, HTTP authentication, Playwright fallback, OCR, LLM analysis, vector retrieval, desktop UI wiring or result export files. Scheduling must reuse the existing SchedulerPort in a later lane-safe integration. Dynamic sources remain classified by the existing HTTP pipeline; browser execution remains a separate explicit fallback decision.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`.
