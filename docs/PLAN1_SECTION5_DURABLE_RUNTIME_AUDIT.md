# Plan 1 Section 5: verified durable runtime and recovery

Independent 5А/5Б/5В and aggregate Section5 are DONE.

| Boundary | Reused implementation and acceptance evidence |
| --- | --- |
| Durable runtime | TaskRuntimeCoordinator, RuntimeSessionStore, incumbent LangGraph adapter and SQLite checkpointer; completed steps and approval interrupts survive object/checkpointer recreation |
| Identity and ownership | Exact task/runtime/thread routes, bounded canonical tokens, persisted storage classes and epochs; foreign-route updates/deletes rejected; one recovery-claim winner |
| Side effects | Existing ToolEffectGuard and SQLite IdempotencyLedger bind operation identity to arguments; completed effects replay without a second handler call; conflicting arguments fail closed |
| Uncertainty | Timeout, cancellation, handler failure and unresolved recovery effects remain uncertain; no automatic fresh replay |
| Retry | Explicit provider retryability and no-effect evidence, positive backoff and bounded monotonic total budget; cancellation and resume-token admission retained |
| Atomic finalization | Injected session/audit failures roll back task state, cursor and audit together; active checkpoint pointers survive injected process-loss exceptions |
| Ledger receipts | Completed result evidence is immutable; equivalent replay is idempotent; exact JSON/storage/identity/timestamps checked within existing SQLite writer transactions |
| Batch recovery | Cursor/input identity, durable key, immutable receipts and frontier reconciled before mutation; failed checkpoint writes reconcile to durable state |
| Wait recovery | Completion deadlines remain the minimum wait for scheduler and legacy snapshots; explicit completion provenance requires exact agreement; later old scheduler waits survive repeated restore |

Canonical repairs were integrated in order #832 -> #850 -> #851 -> #853.
They reuse the existing services; no extra runtime, ledger or scheduler was added.
#853 retains its original four owned paths, with two independently reproduced
deadline defects repaired in the same lineage. The combined candidate also
contains the exact qualified three #850 paths and two #851 paths.

Combined candidate 0ce8ea1905d3d4e0dd7c73f01962f90cebbe8caf passed Core
44107711001 and full M12 44107711069. Local locked verification on the identical
source tree passed 3318 tests / 5 skipped plus dependency, Ruff and compilation
checks. Hosted Windows passed 3316 / 7 skipped. Independent focused review
passed 171 tests and executed SQLite snapshots created by the actual old-main
implementation, both with and without an initial completion deadline.

Main integration 682740c07f50c25fb6d968f037e1977e17dc818e read back tree
e4c26848a6605f67027d1f6fe1b5305fec233d61 exactly equal to the combined
qualified candidate. Prior session/ledger integrations were separately checked
against current-main plus their scoped blobs. The preceding closure documents
were preserved.

M12 packaged Windows job134137077323 verified notices, manifest, UIA/keyboard
and autostart, then reproved the exact extracted distributable. Artifact14340273122
digest54d04a39f7f897a6582fe52bb1f63a45132cdac54644f645883b3fdbd9eb0940;
final distributable ZIP SHA-256
bdcbd9115c88f4dab602ddf45abfc2c210f70add3f8638c3684a37cadee0d86e.

These are repository-controlled intermediate engineering acceptance results.
Existing process-loss tests inject BaseException or recreate runtime objects;
they are not described as physical power-loss proof. A separate new subprocess
loss check was not executed after local execution became unavailable.
Optional live hardware/provider checks and main-only provenance were skipped.
HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false.
Plan7 retains physical/final product obligations.

No production data, credentials, browser profiles, private logs or model weights
were committed. Preserved Git history allows rollback; no user database was
migrated. Drive writes were denied HTTP403; GitHub is the terminal status record.
Next action is Plan1 6А using existing security authority lineages.
