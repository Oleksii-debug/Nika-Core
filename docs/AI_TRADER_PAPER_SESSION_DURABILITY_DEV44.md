# DEV44 — deterministic paper-session durability

Status: IMPLEMENTED on a dependent additive lane; not integrated to `main`.

## Exact dependency boundary

- One-Shot-44 starting live `main`: `3fbfabfc93d59183f174ff44098db886cff93bd8`.
- Live `main` advanced during the cycle to `e8743566ffc673d6f8d272e88de0e027c23ab277`
  through unrelated DEV16 deterministic-planning integration.
- Canonical replay/accounting/risk work is still open in PR #67 at exact head
  `7ee44a34c3358858899bbfd258f33c026a666497`.
- DEV26 held-out metrics/evaluation is still separate in PR #193 at exact head
  `97642b59dbff2d8f685cdf76acb427a61a28243a`.
- DEV44 therefore targets `agent/dev03-replay-accounting-risk` directly. Its diff is additive-only
  and does not modify #67-owned `orders.py`, `replay.py`, `accounting.py`, `risk.py`,
  `persistence.py`, `strategy.py`, or shared `trading_research/__init__.py`.
- DEV44 must not be retargeted/merged to `main` until the canonical Trader kernel is integrated or
  ownership is explicitly transferred and a compatibility rebase is performed.

This is a dependency implementation, not a claim that an unmerged sibling branch is canonical
`main` truth.

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
  application/persistence, preventing otherwise-identical sessions from sharing a dedup identity;
- reconstruct the ledger by replaying the session's committed fills through the existing
  `PortfolioLedger`, then compare the exact reconstructed account snapshot to the durable snapshot;
- preserve the exact risk-approved order object across restart rather than asking strategy code to
  regenerate executable authority.

**CUSTOM(thin)**

- Nika-specific paper-session identity/config binding;
- session-scoped SQLite migration/tables;
- market cursor/slice fingerprint, durable marks, risk/account snapshots, pending/terminal order
  state, and session fill journal;
- atomic per-slice transition and restart integrity checks.

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
selection, partition or promotion object is imported into simulated execution state. DEV26 remains
an evidence/evaluation lane rather than an execution-authority source.

## Risk authority and strategy boundary

The public queue operation accepts an `OrderIntent`, not a `RiskApprovedOrder`.

For the current committed market cursor it:

1. requires intent `submitted_at/submitted_slice` to equal that cursor;
2. reconstructs current account/risk state from durable session state;
3. includes same-instrument durable pending quantity;
4. calls the canonical `RiskEngine.approve` with durable limits/policy;
5. persists only the returned `RiskApprovedOrder`.

Restart restores that exact persisted approved order. Strategy code cannot use restart as an
opportunity to replace a durable approval with a weaker policy or skip canonical risk approval.
Repeated use of the same `intent_id` is idempotent only when the complete intent is identical;
semantic rebinding fails closed.

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

No broker endpoint, broker SDK, funding state, credential, network client, or real-money enable
switch exists in this layer.

## Deterministic slice commit

For a new `TimeSlice`:

1. reject time beyond the durable data cutoff;
2. reject cursor rollback or skipped slices;
3. hash exact slice index/time/events;
4. update deterministic accounting marks from Quote midpoint, Bar close or Tick price;
5. reconstruct the ledger from already committed session fills;
6. process durable nonterminal risk-approved orders in stable queue order;
7. scope each generated fill identity to the session;
8. apply each candidate fill to the reconstructed ledger and run canonical post-fill risk checks;
9. derive exact account snapshot and peak-equity risk state;
10. in one SQLite transaction, insert fills, update all affected order lifecycle rows, and advance
    account/risk/marks/cursor/session state.

If the transaction fails, SQLite rollback leaves none of those changes committed. In-memory session
state is replaced only after a successful durable commit.

A retry of the exact already-committed slice compares the stored SHA-256 and returns a no-op result.
The same index with changed time/events fails closed. If a crash occurred before commit, the durable
cursor did not advance and deterministic execution can safely regenerate the same logical fill.

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
  account snapshot under the persisted marks;
- risk session-start equity equals starting cash;
- risk peak equity is not below current account equity.

Cancelled and expired orders remain terminal and are excluded from the executable set after restart.

## Adversarial test families

`tests/test_trading_research_paper_session_restart.py` covers:

1. session/config/data/account/risk/cursor restart identity;
2. crash after durable queue but before latency activation;
3. crash after partial-fill commit and same-slice retry without duplicate fill;
4. cancelled and expired terminality across restart;
5. durable risk limits after restart and absence of a public approved-order queue bypass;
6. dataset semantic fingerprint/cutoff mismatch and cutoff enforcement;
7. simulated SQLite crash during the final session-row update, proving fill/order/session rollback;
8. exact long -> short reversal with Decimal cash/fees/P&L/equity/gross/net invariants;
9. same-slice multi-order fill reconstruction in durable queue order with path-dependent
   realized P&L and average basis;
10. two sessions producing the same engine raw fill identity without cross-session dedup collision;
11. durable order/config tamper fail-closed;
12. no held-out/promotion/real-money config surface and no network-client imports.

The local isolated harness used during implementation passed all 12 focused tests. Repository CI on
the exact GitHub candidate remains the acceptance authority.

## Acceptance truth

- REAL_MONEY_AUTHORITY=false
- BROKER_ADAPTER=false
- NETWORK_EXECUTION=false
- HELDOUT_EXECUTION_AUTHORITY=false
- HUMAN_TESTED=false
- NVDA_VERIFIED=false
- INTEGRATED=false
- NO_SELF_MERGE=true

The next integration step is dependency-driven: refresh against the integrated/current successor of
#67, replay exact Core/M12 plus the Trader numerical-oracle family and DEV44 restart family, then
obtain independent audit/TECH02 compatibility evidence before main integration.
