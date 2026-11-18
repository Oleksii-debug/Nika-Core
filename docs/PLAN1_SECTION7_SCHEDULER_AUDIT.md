# Plan 1 Section 7: scheduler convergence evidence

## Candidate and boundaries

The scheduler finisher reuses existing PR #994, dependent on Resource #357 exact
`4a7f05f3762a7e4fc3c45ff1e60f1a88e2c82329`, tree
`3464f48246d03dad10c4e087533e22c28351f41f`. Local baseline commit
`a063b91cfd125917c52856e2b771d5d43e67bbd9` has the identical tree.
The aggregate tree adds only scheduler source, its narrow connectivity caller,
associated tests, and this evidence document to that frozen baseline.

This is implementation/qualification preparation, not Section 7 DONE. Resource
budgets and profiles retain their own canonical implementation. No database schema,
SQLiteStore/recovery implementation, UI, package, dependency, or release workflow
is changed by this scheduler lane.

## Reuse and compatibility decisions

| Existing lineage | Exact source reused | Adaptation against the frozen baseline |
| --- | --- | --- |
| #990/#988 -> #992 -> #993 -> #994 | `ddf47498f66d2bc8781376dd84ab375f91e5d3d9` | Persisted-only activation, bounded post-install successor reconciliation, stale-dispatch liveness, and stopped-runtime fence. Preserve current #884 transactional dispatch/task guards and #1107 typed `_same_job_snapshot` at every snapshot comparison, including the live-runtime fast path. |
| #1042 -> #1044 -> #1046 | `f85b7b50fd0068ccd06b42c091655cfb4a25c535` | Reuse only `list_enabled` SQL filtering/preflight and the existing decoder. Disabled history, corrupt active BLOBs/JSON, and unused timestamp columns do not cross the Python row boundary. |
| #876 recurrence | `8215dda9ecfa444a9c5a6bc3c5c76bacc8b69027` | Reuse incumbent service and tests for exact carriers, trusted aware UTC, canonical immutable replay identity, persisted timeline validation, resolver-side cancel/pause/deadline fencing, terminal convergence, and missing/terminal-task suppression recovery. |

The current `ConnectivityWaitService` already commits task/job/audit atomically;
only `_activate_runtime` changes. It calls the declared `SchedulerPort`
`activate_persisted` operation and never writes its potentially obsolete snapshot
again. Unsupported older ports raise a controlled error while keeping the durable
wait available to real scheduler restart; there is no unsafe `upsert` fallback.
Surrounding retry/session/time/DTO behavior remains current baseline code.

Do not merge the historical ancestor PRs independently. The final aggregate must
be qualified as one tree and eventually squash-integrated so unqualified historical
ancestor trees are not introduced into main as release states.

## Reproduced failures and checked behavior

The five actual donor modules executed against unchanged frozen production source
first: **48 failed, 26 passed (74 cases)**. Failures reproduced missing activation,
shutdown/runtime resurrection, missed successor installation, disabled/oversized
row hydration, recurrence replay aliasing, behavioral carriers, and lifecycle/time
validation gaps. These are actual repository pytest executions, not isolated mocks
of the intended implementation.

After adaptation the focused scheduler/store/recurrence/connectivity/current
dispatch-authority/resource compatibility suite passed **200 tests**. It includes:

- Existing #884 transaction and #1107 bool/int/float identity tests.
- Real SQLite reopen/Unicode paths, corrupt active columns and disabled history;
  row-factory probes verify rejection before materialization.
- Deterministic shutdown/installation races and bounded successor churn.
- INTERVAL cadence preservation when an already-correct successor is installed.
- New bool/int/float successor tests at successful/failed installation and live
  runtime fast-path boundaries; all rejected stale occurrences have zero resolver
  observations.
- Actual connectivity defer-to-adapter activation with a replacement between
  durable commit and activation, both running and deferred-until-restart paths.
- Unsupported-port failure, retained durable WAITING_TOOL, real-adapter restart.

Two donor fixture adaptations preserve current contracts: the non-finite persisted
payload test checks the store's corruption wrapper and its non-finite cause; the
existing typed runtime-sync test explicitly starts/stops its adapter before testing
live reconciliation. No production safety or error gate was weakened.

## Qualification state

Local full pytest passed **3925 passed, 5 skipped, 11 warnings (106.32 seconds)**.
The normal `python -m nika_core` startup also passed against a new temporary
database in a Unicode/space-containing directory. This is Linux source evidence.
Exact hosted Core/M12 Ubuntu and Windows, packaged Windows proof, independent
changed-head review, and post-merge readback remain to be recorded against the
resulting aggregate SHA. Earlier donor CI does not qualify this tree.

Repository Ruff, compile of source/tests/scripts, `pip check`, and `git diff --check`
passed using the existing isolated hashed Core/build interpreter. No dependencies
or shared interpreter metadata were modified. Skipped tests are not awarded PASS.

`IMPLEMENTED=true; INTEGRATED=false; PACKAGED=false; HUMAN_TESTED=false;`
`NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false; SECTION7_DONE=false`.

No secrets, credentials, production databases, private logs, browser profiles,
model weights or large generated files are included. Next step: independent review
and fresh exact-head hosted qualification of the composed candidate, followed by
the owner's ordered integration/acceptance process.
