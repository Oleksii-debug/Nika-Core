# Universal Research recurring schedules and deltas

Status: DEV01 implementation contract. This document does not claim UI completion, HUMAN_TESTED, or NVDA_VERIFIED evidence.

## Capability

`ScheduledResearchProfileService` composes immutable Research profiles with Nika Core's existing `SchedulerPort`. It does not implement a second scheduler. A schedule stores only stable Research identifiers (`series_id`, `profile_id`, optional exact `profile_version`) and uses `max_instances=1` plus the existing scheduler's coalescing/misfire controls.

When `profile_version` is omitted, every new scheduler occurrence resolves the latest profile version at task creation. When an exact version is supplied, recurring runs keep using that immutable version. Each created run still pins the exact profile and source-set versions through `ResearchProfileRunService` before any refresh work starts.

## Durable recurring lineage

Migration 13 adds Research-owned tables for:

- recurring series to canonical TaskQueue task bindings;
- completed profile-run history and previous-result lineage;
- persisted `new` / `changed` delta rows.

The scheduler action first reconciles an existing bound non-terminal task for the same series. A completed task whose result exists but whose delta-history write was interrupted is finalized into history instead of creating and executing a second Research task. Explicitly paused Research tasks stay paused; scheduler invocation does not silently resume them.

The series binding is persisted after canonical `TaskQueue.create_job()` has returned READY and before the run performs source refresh work. A process failure inside that narrow pre-binding interval can leave an orphan READY task, but it cannot duplicate a completed fetch/extraction side effect because no run work has started. Avoiding even that orphan record would require a shared TaskQueue atomic-create extension and is intentionally outside DEV01 ownership.

## Delta semantics

The first completed run in a series reports every matching result as `NEW`. Later runs omit exact `document_id` matches. A different document reached through the same stable `(source_kind, source_id)` identity is `CHANGED` and stores `previous_document_id`. A different stable source identity is `NEW`.

Locator changes such as an HTTP redirect target change do not turn an update into a false `NEW` result because `source_id`, not final locator text, is the version-lineage identity.

The text projection reports only new/changed items and ordinary provenance text so later accessible UI/report adapters do not need a second data model.

## Current HTTP snapshot correctness

A source-scoped deterministic FTS query now treats an HTTP source's `current_raw_sha256` as its current version pointer and accepts only the snapshot with that raw digest. Older snapshots remain in the corpus for provenance/history, but they no longer appear as simultaneous current results merely because the same `source_id` historically produced them.

This preserves last-good behavior: source-scoped queries can still use the current persisted snapshot after a failed refresh; profiles that require a specific freshness state can additionally filter on the source freshness field.

## REUSE / ADAPT / CUSTOM

- **REUSE:** `SchedulerPort`, `ScheduledJob`, canonical SQLite migrations, TaskQueue, CheckpointService, durable profile runs, incremental HTTP refresh, deterministic FTS5, provenance/result storage.
- **ADAPT:** HTTP source-scoped query semantics use the existing `current_raw_sha256` pointer instead of accepting historical snapshots.
- **CUSTOM (thin):** Research-owned recurring series/task lineage, delta persistence/classification, schedule composition, and accessible text delta projection.

No new dependency, browser engine, OCR implementation, vector store, LLM path, database, runtime kernel, or scheduler is introduced.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`.
