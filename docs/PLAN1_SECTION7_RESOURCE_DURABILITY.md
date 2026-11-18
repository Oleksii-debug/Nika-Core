# Plan 1 Section 7 — resource-only durability candidate

Status: IMPLEMENTED candidate; not Section DONE, integrated, packaged, human tested or NVDA verified.

## Canonical reuse and compatibility

This is the resource-only continuation of owner PR #357. Source baseline is exact
typed scheduler candidate #1107 `2d688ce453efa83b145b05a448d37af683449250`
(tree `f1d597668e9a5989b4bb754aee5dbcfcabf334cd`). It adapts the durable
resource request/FIFO/process-generation semantics from historical #357
`add292566e15c5d8b6944ced7b03ea3b8ec82bf5`; it does not import that branch's
Memory, scheduler, expanded budget or disk/GPU snapshot contracts.

The current five-field ResourceBudget and eight-field ResourceSnapshot stay intact.
Current telemetry/admission/live-grant revalidation from #1341
`e945b53ab2d622d73436c880256755c22981803f` and existing-adapter validation from
#1114 `c48be63f45dcbd6058c118f8fa025cea26ad57e6` are retained.
The canonical observer takes a bounded 0.1-second measured CPU sample, preventing
psutil's first nonblocking zero from becoming admission evidence. Packaged admission
samples on the existing background runtime loop, not the bridge/UI thread.
Official API basis: https://psutil.readthedocs.io/stable/#psutil.cpu_percent

REUSE: canonical SQLiteStore/SQLite transactions and the existing psutil adapter.
ADAPT: durable FIFO requests, typed request/process identities, owner-generation proof,
current telemetry validation and the existing DesktopBackend execution boundary.
CUSTOM(thin): Nika admission/ownership/FIFO policy and its narrowly versioned schema.
No second resource manager, scheduler, persistence host or observer is introduced.

## SQLite authority and upgrade decision

New uniquely named `resource_durability_schema_migrations` version 1 creates only
`resource_requests` and its queue/owner indexes, including PID and process-start
columns. This is not historical mixed `m3_extension_schema_migrations` v1 or v2:
neither scheduler bindings nor disk/GPU/RSS budget columns are represented as applied.
The existing core resource_budgets table and all budget rows remain authoritative.

SQLiteStore.initialize applies the resource-only stream inside its canonical
BEGIN IMMEDIATE initialization transaction. Empty, historical core-only stores
upgrade additively. Future/noncontiguous versions, orphaned request tables, missing
columns/tables/indexes and missing FIFO/state constraints fail closed. An unmarked
historical mixed-M3 resource_requests table is deliberately not silently adopted;
its bytes/state require explicit compatible reconciliation. Isolated migration
fixtures with no resource prerequisite or derived state remain valid.

Legacy-location inspection recognizes and validates the new marker, counts only
its migration rows as empty bookkeeping, and treats actual requests as user data.
No legacy adoption, backup, restore, rollback, metadata or recovery lease authority
is replaced. Ordinary backup/restore still uses the canonical recovery service.

## Runtime and ownership behavior

All request/grant/FIFO/count truth is SQLite-authoritative. Independent manager
instances serialize grant decisions through BEGIN IMMEDIATE. Terminal requests
rejoin behind existing waiters. ProductProject request binding cannot drift.
Only grants acquired by this instance and matching its durable owner/process
generation can be released or revalidated; a reused textual manager ID cannot
adopt a prior grant.

Restart release requires ResourceOwnerProbePort to independently prove the exact
persisted PID and creation-time generation dead. Caller stale_manager_id is only a
selector. No probe, live owner, inspection failure, non-bool proof, missing/corrupt
generation or mixed generations preserve capacity and fail closed. AccessDenied
means alive; PID reuse is distinguished by process creation time.

The packaged builder now invokes that guarded reconciliation before creating the
desktop backend. A real child-process grant followed by os._exit reproduced a
startup capacity leak before this composition was added. Startup recovery never
replays tasks or sessions; live, unknown and corrupt owners keep their leases.
Recovery audit contains bounded outcome/count fields, without private owner IDs
or probe exception text. Fresh-start execution is verified after actual process
loss, and live/unknown/corrupt negative startup paths remain nonexecuting.

The audited production source baseline has no ResourceManager constructor:
MediaResourceCoordinator accepts an injected manager, while the packaged builder
previously omitted admission entirely. build_windows_bridge now injects the real
PsutilResourceObserver and ResourceManager over the same canonical store as its
TaskQueue. DesktopBackend gates actual fresh start and saved resume before runtime
effects with one shared desktop_runtime/local_machine capacity scope. Finally
release follows operation settlement, including failure and cancellation.

Denied fresh starts remain PAUSED without a runtime session; denied saved resumes
preserve their existing cursor. Existing explicit task.resume retries, and local
cancellation removes the durable wait. Task text shows a bounded resource message
through the existing textContent task renderer. Isolated DesktopBackend callers
may explicitly omit the optional manager, preserving existing unit composition.
Packaged composition always supplies it; there is no in-memory production fallback.

This gate covers the packaged ordinary-task execution boundary. It does not claim
that every ProductFactory/loop/media execution is now admitted; those callers must
use the existing manager injection ports and their own qualified composition.

## Qualification and integration boundary

Local isolated materialization of the exact baseline plus owned paths passed
compileall, owned Ruff and 165 focused tests, including shared-manager concurrency,
FIFO restart, live-owner denial, absent/uncertain probe, manager-ID reuse, corrupt/
mixed generations, current telemetry, optional metrics and actual desktop
start/resume/cancel/failure behavior. This is not full locked-repository CI or
physical Windows/NVDA evidence.

The dependency owner must promote the already-reviewed psutil runtime into the GUI/
packaged lock profile and its required runtime notice inventory before qualification.
Fresh final-head Core Ubuntu/Windows, M12 integrated Ubuntu/Windows, packaged Windows,
and independent review are required. Prior SHA results are lineage only.

The source publication retains old #357 and frozen #1107 parents solely to preserve
history. Final integration MUST use a reviewed squash of the scoped tree delta;
an ordinary ancestry merge would carry unrelated old lineage. Keep PR #357 draft.
