# Plan 5 §1 — Trader PAPER workspace query boundary (component, NOT terminal)

## Authority and reuse

This adapter belongs only to Plan 5 Section 1 and consumes the existing
`TradingStateRepository` (canonical SQLite paper accounting) plus the existing
`UIActionBridge` read-only `StateProvider` contract. It adds no broker,
order-dispatch path, policy evaluator, runtime, scheduler, permission database,
memory store, account ledger or credential access. No real-money authority.

`PaperWorkspaceQuery(repository, authorize_read=...)` requires a **trusted
host-supplied Core authorization callback**, evaluated for the exact pair
`(workspace_id, run_id)` both before SQLite access and before returning
data. A browser, language model, or UI command must **never** supply that
callback or select its trusted scope. Both scope IDs must be exact plain strings,
nonblank, already trimmed, printable (no control, line separator or bidi
formatting), and at most 512 UTF-8 bytes each **before** the callback or
SQLite query. Invalid trusted-host scope is `ACCESS_DENIED`, never storage
unavailability or a fabricated empty account. The host must resolve the currently
authenticated user/workspace/run and consult the existing Core permission
authority, including revocation. Only singleton boolean `True` grants read.

`paper_state_provider(query, host_scope=...)` adapts it to the existing
`UIActionBridge(state_provider=...)`. Host-side construction owns the scope;
there is no client-requested tenant selector. On success the returned state
contains text-first `mode=PAPER_ONLY`, `state=PAPER_DATA` or
`NO_PAPER_DATA`, currency/position fields, and decimal strings suitable
for semantic tables and NVDA. It never returns executable domain objects.
Denied, malformed or revoked access is `ACCESS_DENIED`; corrupt/unavailable
paper evidence is `EVIDENCE_UNAVAILABLE`, **not** an empty account or
a fabricated zero balance. Unsafe bidirectional/control identity text is
rejected before operator projection.

## Automated tests committed

`tests/test_plan5_trader_workspace_query.py` covers negative/foreign-run
isolation, exact authorization bool, callback faults, mid-read revocation,
hostile ID carriers, normal empty state, durable SQLite fill/restart,
corrupt durable payload, unsafe operator identity and integration with
the existing UIActionBridge state provider.

## Gaps requiring future Plan 5 Section 1 work

This is a **read-only, repository-controllable component**, not a completed
Trader workspace or section. Host composition with real Core policy,
registered accessible routes/commands, approval/evidence/report workflows,
durable pending-order/replay recovery, recurring observations and combination/
time-wave trading research remain required. Pending stacked Trader PR ancestry
must qualify on exact-head Ubuntu/Windows CI, integrate in order to main,
and receive readback before any terminal DONE. A fixture projection is not
physical NVDA or real broker/market evidence. Plan 5 Section 2 remains
unstarted while Section 1 is unfinished.


## Additional projection-integrity fence (child implementation)

The read-model re-admits exact built-in JSON-like account and position
containers when converting canonical SQLite evidence to the presentation-neutral
PAPER state. The source decoder is authoritative for account invariants; this
last-mile fence prevents a faulty/replaced read adapter from leaking custom
behavioral carriers into Windows UIA, Web semantic tables, or text reports.
It requires the known account and position field sets, exact plain UTF-8
numeric strings of at most 128 bytes, finite Decimal parse, one-line,
trimmed, printable position identifiers up to 512 bytes and a plain
three-letter uppercase ASCII currency. Positions beyond the existing
100,000-row durable limit fail closed rather than being silently truncated.
Existing valid decimal strings remain unchanged so no balance is fabricated
or rounded.

On malformed evidence, the existing provider returns EVIDENCE_UNAVAILABLE
without balances/positions. A fresh Core authorization check is performed on
this failure path too; revoked permission takes precedence as ACCESS_DENIED.
No additional policy evaluator, persistence, runtime, scheduler or domain
agent is introduced. Focused regression coverage:
`tests/test_plan5_trader_projection_integrity.py`. This is a **bounded
component hardening step**; it neither integrates the stacked Trader lineage
into main nor closes the full AI Trader section.
