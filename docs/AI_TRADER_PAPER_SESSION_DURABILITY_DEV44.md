# DEV44 — deterministic paper-session durability

Status: REPAIR IMPLEMENTED on a dependent additive lane; fresh exact-head acceptance pending.

## Exact dependency boundary

- One-Shot-44 original starting live `main`: `3fbfabfc93d59183f174ff44098db886cff93bd8`.
- Latest live `main` at this repair cycle read: `af43e41dca1066f95debafef360d61b2bf38b2ec`.
- Canonical replay/accounting/risk work is still open in PR #67 at exact head
  `7ee44a34c3358858899bbfd258f33c026a666497`.
- DEV26 held-out metrics/evaluation remains a separate active lane in PR #193 and is not imported
  into paper-session execution authority.
- DEV44 therefore still targets `agent/dev03-replay-accounting-risk` directly. Its diff remains
  additive relative to #67 and does not modify #67-owned `orders.py`, `replay.py`, `accounting.py`,
  `risk.py`, `persistence.py`, `strategy.py`, or shared `trading_research/__init__.py`.
- DEV44 must not be retargeted/merged to `main` until the canonical Trader kernel is integrated or
  ownership is explicitly transferred and a current-main compatibility rebase is performed.

This is a dependency implementation, not a claim that an unmerged sibling branch is canonical
`main` truth.

## Superseded exact-green lineage

DEV44 head `66b620e38abb0149bfe3acfcec6882f485b47d22` passed hosted gates before this
repair:

- Core CI #1728 / run `32667310245`: SUCCESS on Ubuntu and Windows, exact checkout identity,
  dependency consistency, Ruff, compile and complete pytest (`710 passed`, 3 pre-existing warnings
  on each platform).
- M12 #1495 / run `32667310353`: SUCCESS for Ubuntu integrated system proof, Windows integrated
  system proof and Windows packaged release proof.

That SHA is **superseded for DEV44 acceptance**. A subsequent adversarial source review found two
DEV44-owned authority defects that hosted tests did not cover:

1. pre-trade `queue_intent(..., mark_price=...)` accepted a risk valuation supplied by the
   strategy/caller, allowing an intent to obtain a `RiskApprovedOrder` against an artificially low
   price rather than the durable committed market cursor;
2. per-slice post-fill drawdown checks used the previous durable `peak_equity` and recorded the
   current slice's mark-to-market peak only after all fills, so a peak-to-fill drawdown occurring
   inside one slice could be understated.

No acceptance or integration credit may use `66b620e...` after those defects were identified.

## REUSE -> ADAPT -> CUSTOM(thin)

**REUSE**

- canonical `SQLiteStore` and its transaction/rollback semantics;
- exact `Decimal` Trader accounting types and `PortfolioLedger`;
- `OrderIntent`, `ExecutionPolicy`, `RiskApprovedOrder`, `SimulatedFill`, and lifecycle states;
- `SimulationExecutionEngine` and `TimeSlice` causality rules;
- `RiskEngine`, `RiskLimits`, and `RiskState`;
- `DatasetVersion` semantic/raw hashes and source provenance.

**ADAPT**

- scope deterministic engine `fill_id` values to a durable paper-session identity before account
  application/persistence;
- reconstruct the ledger by replaying committed fills through the existing `PortfolioLedger`, then
  compare the exact reconstructed account snapshot to durable state;
- preserve the exact risk-approved order across restart rather than asking strategy code to
  regenerate executable authority;
- derive pre-trade risk valuation exclusively from the session's durable mark for the current
  committed market cursor.

**CUSTOM(thin)**

- Nika-specific paper-session identity/config binding;
- session-scoped SQLite migration/tables;
- market cursor/slice fingerprint, durable marks, risk/account snapshots, pending/terminal order
  state and session fill journal;
- atomic per-slice transition and restart integrity checks;
- current-slice peak-equity tracking around post-fill risk assertions.

No new dependency is introduced.

## Durable authority

`PaperSessionConfig` binds:

- `session_id`;
- strategy identity/version;
- exact starting cash;
- dataset ID/version, raw hash, semantic hash, source ID, acquisition time, license reference and
  cutoff;
- exact `ExecutionPolicy` including integer-microsecond latency, slippage, fees and liquidity
  fraction;
- exact `RiskLimits`.

The config is canonical JSON plus SHA-256 and is immutable after creation. Resume loads durable
config rather than accepting caller-supplied replacement risk limits or execution policy. An
optional `expected_data` binding rejects restart against a changed dataset fingerprint/cutoff.

Held-out research evidence is deliberately absent from this config and schema. No held-out metric,
selection, partition or promotion object is imported into simulated execution state.

## Risk authority and strategy boundary

The public queue operation accepts only an `OrderIntent`; it no longer exposes a `mark_price`
argument.

For the current committed market cursor it:

1. requires intent `submitted_at/submitted_slice` to equal that cursor;
2. obtains the intent instrument's valuation only from the durable session mark produced by the
   committed market slice;
3. fails closed if that cursor has no durable mark for the instrument;
4. reconstructs current account/risk state from durable session state;
5. includes same-instrument durable pending quantity;
6. calls canonical `RiskEngine.approve` with durable limits/policy and the durable mark;
7. persists only the returned `RiskApprovedOrder`.

A strategy therefore cannot choose a favorable risk price or submit a pre-built approval. Restart
restores the exact persisted approved order. Repeated use of one `intent_id` is idempotent only when
the complete intent is identical; semantic rebinding fails closed.

## SQLite schema

DEV44 uses the canonical Nika SQLite database and an independently versioned session migration
family so it does not change PR #67's migration version while #67 still owns that file.

Tables:

- `trading_research_paper_schema_migrations`;
- `trading_research_paper_sessions`;
- `trading_research_paper_orders`;
- `trading_research_paper_fills`.

The session row stores exact account/risk/marks/cursor state plus a state digest and optimistic
`row_version`. Approved-order immutable payload and mutable lifecycle state have separate digests.
Each fill has immutable JSON plus digest and is keyed by `(session_id, fill_id)`.

No broker endpoint, broker SDK, funding state, credential, network client or real-money enable
switch exists in this layer.

## Deterministic slice commit and risk peak semantics

For a new `TimeSlice`:

1. reject time beyond the durable data cutoff;
2. reject cursor rollback or skipped slices;
3. hash exact slice index/time/events;
4. update deterministic accounting marks from Quote midpoint, Bar close or Tick price;
5. reconstruct the ledger from already committed session fills;
6. compute the account at those new marks **before any current-slice fill** and raise the in-memory
   risk peak to that mark-to-market equity when it is a new high;
7. process durable nonterminal risk-approved orders in stable queue order;
8. scope each generated fill identity to the session;
9. after each candidate fill, compute exact account state and run canonical post-fill risk checks
   against the current in-slice peak;
10. after a safe fill, advance the in-slice peak if that fill produced a still-higher equity;
11. derive the final exact account/risk state;
12. in one SQLite transaction, insert fills, update affected order lifecycle rows, and advance
    account/risk/marks/cursor/session state.

If a post-fill risk assertion fails, no slice transaction is attempted and durable cursor/fills/order
state remain at the previous committed boundary. If the transaction itself fails, SQLite rollback
leaves none of its changes committed. In-memory session state is replaced only after a successful
durable commit.

A retry of the exact already-committed slice compares the stored SHA-256 and returns a no-op result.
The same index with changed time/events fails closed. If a crash occurred before commit, the durable
cursor did not advance and deterministic execution can regenerate the same logical fill.

## Restart reconstruction invariants

Resume rejects state when any of these are false:

- config digest matches and session identity is unchanged;
- cursor metadata is internally consistent;
- session state digest matches;
- order queue sequence is contiguous;
- each approved-order payload digest and mutable-state digest matches;
- no rejected order is executable durable session state;
- each fill payload/digest/reference matches its approved order;
- each fill has the correct session scope;
- cumulative fills never exceed approved quantity;
- durable `remaining_quantity == approved quantity - cumulative fills`;
- no fill occurs after the order's durable last update slice;
- replaying all durable fills through canonical `PortfolioLedger` reproduces the exact persisted
  account snapshot under persisted marks;
- risk session-start equity equals starting cash;
- risk peak equity is not below current account equity.

Cancelled and expired orders remain terminal and are excluded from the executable set after restart.

## Adversarial test families

`tests/test_trading_research_paper_session_restart.py` now covers 14 focused families:

1. session/config/data/account/risk/cursor restart identity;
2. crash after durable queue but before latency activation;
3. crash after partial-fill commit and same-slice retry without duplicate fill;
4. cancelled and expired terminality across restart;
5. durable risk limits after restart and absence of a public approved-order queue bypass;
6. **caller risk-price removal:** the strategy-facing queue signature has no `mark_price` and a high
   durable mark trips gross-exposure policy instead of accepting a spoofed low valuation;
7. dataset semantic fingerprint/cutoff mismatch and cutoff enforcement;
8. simulated SQLite crash during the final session-row update, proving fill/order/session rollback;
9. exact long -> short reversal with Decimal cash/fees/P&L/equity/gross/net invariants;
10. **intra-slice drawdown authority:** a Bar close creates a new mark-to-market peak before an
    existing sell fills at the Bar open; the peak-to-fill loss breaches `max_drawdown`, the slice is
    rejected and restart proves no second fill/cursor advance committed;
11. same-slice multi-order fill reconstruction in durable queue order with path-dependent realized
    P&L and average basis;
12. two sessions producing the same engine raw fill identity without cross-session dedup collision;
13. durable order/config tamper fail-closed;
14. no held-out/promotion/real-money config surface and no network-client imports.

Fresh focused and hosted execution evidence is required on the repair head. The previous 12-test
local/hosted lineage does not qualify the new source.

## Acceptance truth

- REAL_MONEY_AUTHORITY=false
- BROKER_ADAPTER=false
- NETWORK_EXECUTION=false
- HIDDEN_ENABLE_SWITCH=false
- HELDOUT_EXECUTION_AUTHORITY=false
- GREEN=false until the repair commit receives fresh exact-head evidence
- READY_FOR_AUDIT=false until fresh exact-head evidence
- READY_FOR_INTEGRATION=false
- HUMAN_TESTED=false
- NVDA_VERIFIED=false
- INTEGRATED=false
- NO_SELF_MERGE=true

The next integration step remains dependency-driven: qualify the repair exact head, obtain
independent audit, then refresh against the integrated/current successor of #67 and current `main`,
replay Trader numerical oracles plus DEV44 restart/risk tests, and only then consider main
integration.
