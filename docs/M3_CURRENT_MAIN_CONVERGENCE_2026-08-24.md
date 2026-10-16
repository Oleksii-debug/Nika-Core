# M3 Memory / Scheduler / Resource Durability — current-main convergence

Date: 2026-08-24.
Lane: ONE-SHOT-27 / MANUAL-DEV22 successor.

## Canonical base and lineage

This successor starts from exact live main `af43e41dca1066f95debafef360d61b2bf38b2ec`, the guarded merge of PR #158. The final production semantics from historical PR #196 head `4cf3d519cebe20ea9ea0d7eace90481012e074e0` were carried onto the fresh current-main branch through compatibility transport PR #348. The transport changes only the established M3 durability family; it is not production integration and old-head CI remains lineage only.

Direct current-main comparison before this documentation commit had merge-base exactly `af43e41dca1066f95debafef360d61b2bf38b2ec`, `behind=0`, and exactly the 17 established M3 source/test/document paths. Current-main drift since the prior merge base changed deterministic-planning, packaged ProductProject journey and related files but had zero M3 path overlap.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE: APScheduler 3.x remains the sole scheduler engine behind `SchedulerPort`; psutil remains Windows/process observation; SQLite remains authoritative memory/schedule/resource truth.
- ADAPT: durable Nika schedule identity and ProductProject binding over APScheduler; psutil disk/RSS plus PID + process-creation-time liveness proof; canonical SQLite transaction/migration machinery for M3 extension state.
- CUSTOM(thin): memory scope/retention/approval semantics, exact schedule ownership/dedup, FIFO resource requests, budget/fairness policy, process-generation restart recovery and fail-closed validation.

No second scheduler, persistence framework, resource lease service, heartbeat authority, GPU library, permission grant or approval bypass is introduced.

## Closed durability families

Memory:
- short-term/task/thread/agent/workspace/user scopes remain isolated;
- long-term user writes require explicit approval on every write;
- TTL and max-record retention are deterministic and restart-safe;
- `MemoryService.put()` returns the row materialized inside its own write transaction, preserving exact writer return identity under same-key concurrent overwrite.

Scheduler:
- durable `ScheduleIdentity(scope, owner_id, dedup_key, product_project_id)` survives restart and cannot be silently cleared/rebound;
- duplicate owner-local identities fail closed;
- timezone-aware date/start/end semantics are validated before persistence;
- APScheduler-native trigger construction is also preflighted before durable upsert/resume, so invalid cron/interval/timezone definitions cannot poison SQLite through the production adapter;
- pause/resume preserves ProductProject identity and audit state reflects the actual disabled/enabled state;
- definition dedup is not represented as exactly-once external-effect authority.

Resources:
- SQLite-authoritative FIFO requests and `BEGIN IMMEDIATE` grant serialization;
- CPU/RAM/disk/RSS limits and explicit fail-closed GPU-unavailable semantics;
- durable ProductProject request identity;
- restart recovery treats caller `stale_manager_id` only as a selector;
- release requires independent exact PID + process-creation-time liveness proof;
- live owner, no probe, missing/corrupt/mixed generation identity or inspection uncertainty fails closed;
- textual manager-ID reuse cannot adopt/release a previous process generation;
- Windows psutil disk path is passed as `str(path)`.

Schema:
- additive M3 extension schema remains inside canonical `SQLiteStore` initialization;
- future extension versions fail closed;
- missing/corrupt prerequisite state does not silently downgrade recovery authority.

## Independent AUD03 lineage

QA-only PR #265 independently replayed both key adversarial families against historical production head `685a1a8510e3b3bd91ad9ff596bca41f77e27850`: concurrent memory writer-return linearizability and live-resource-owner restart recovery. Its exact QA head `d0e7e0096e5cfe9088c325c50f36a4ce4f8b9f3e` passed Core and complete M12. That evidence is useful lineage but does not self-classify this current-main successor.

The later scheduler-only repair at `4cf3d519...` did not change memory/resource code. Nevertheless, exact-SHA policy still requires independent AUD03 replay or explicit reclassification on the final current-main successor before integration.

## Acceptance rule

The current-main successor receives no integration credit until all of the following are bound to its exact final SHA:

1. dependency consistency;
2. Ruff / compile-import checks;
3. full Core CI on Ubuntu and Windows;
4. complete applicable M12 including packaged Windows proof;
5. independent AUD03 PASS or PASS_WITH_REPAIRS for the live-owner and memory-return families;
6. immediate live-main/head/mergeability compatibility reread;
7. guarded main merge only if no newer conflicting owner or shared-contract change invalidates the evidence.

`IMPLEMENTED=true` for the source family. `GREEN`, `INTEGRATED`, `PACKAGED`, `HUMAN_TESTED`, `NVDA_VERIFIED`, and `PRODUCTION_RELEASE_READY` must reflect only exact executable evidence; automation never grants human/NVDA status.
