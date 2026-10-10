# AI Trader Research Lab — Batch 2 replay, accounting, risk

Starting integrated main: `782105eba05da9714c30d45483049dbb1fe06370`.
Current-main compatibility refresh base: `9140389d552dc56c3ca39b11ab95a534abc44eef`.
Latest reservation repair starting live main: `109829579ab4693e038e218769c23c2547defd64`.

## Scope

This batch extends the integrated causal/data foundation with a paper-only deterministic replay and accounting kernel. It stays inside DEV03 ownership: `trading_research`, directly coupled tests, fixtures and this document.

## Safety architecture

The executable chain is intentionally one-way:

`DecisionContext -> OrderStrategy -> OrderIntent -> RiskEngine -> RiskApprovedOrder -> SimulationExecutionEngine`

`OrderIntent` is not accepted by the simulation execution engine. The engine accepts only `RiskApprovedOrder`. No broker SDK, brokerage adapter, send-order method, network order route, hidden enable flag or real-money execution capability exists.

A newly proposed order cannot fill on the same `TimeSlice` that produced it, even when configured latency is zero. Approval-slice fills are also blocked. Time slices fail closed if they contain information whose `available_at` is later than the slice time.

## Deterministic replay

The phase order is fixed:

1. MARKET_DATA
2. EXISTING_ORDERS
3. ACCOUNTING
4. STRATEGY
5. RISK
6. QUEUE_NEW_ORDERS

Market/limit v1 execution supports PENDING, ACTIVE, PARTIALLY_FILLED, FILLED, CANCELLED and EXPIRED lifecycle states. Quote execution uses ask for buys and bid for sells. Bar execution uses explicit v1 open/limit rules. Liquidity is explicit through quote sizes or bar volume and a versioned maximum fill fraction. Latency, slippage, percentage fee and fixed fee are versioned in `ExecutionPolicy`.

`fixed_fee` is an approved-order charge, not a per-partial-fill charge. The first economic fill of an approved order carries the fixed fee; later partial fills carry percentage fees only. This gives deterministic pending reservation a bounded fixed component while preserving percentage fees proportional to executed notional.

## Decimal accounting

Authoritative simulated money and position calculations use `decimal.Decimal`: cash, fees, signed position, average basis, realized/unrealized P&L, equity, gross exposure and net exposure. Long and short adds, reductions, closes and reversals use deterministic average-cost accounting.

## Risk and reservation authority

`RiskEngine` evaluates current state plus accepted-pending state plus the candidate before creating `RiskApprovedOrder`. Limits cover absolute position, gross exposure, net exposure, cash/buying power, session loss, drawdown, short policy and leverage. Post-fill assertions recheck position, exposure, leverage and loss/drawdown boundaries.

The preferred pending boundary is `PendingRiskOrder`, which binds the exact accepted `RiskApprovedOrder`, its current mark and its exact remaining quantity. Risk recomputes deterministic pending economics from the canonical order policy instead of trusting a caller-supplied fee total. Already-filled quantity is not counted again; when a pending order is partially filled, its once-per-order fixed fee is treated as already paid.

The old `pending_signed_quantity` input remains only for zero-cost compatibility. If deterministic slippage, percentage fees or a fixed fee are configured, a non-zero aggregate pending quantity is insufficient to reconstruct the number and policies of accepted orders and therefore fails closed with an exact-pending-reservation requirement.

Candidate and exact-pending execution costs reduce projected equity before leverage, session-loss and drawdown admission. BUY notional plus deterministic fees is reserved from current cash; future SELL proceeds are not treated as available buying power for another pending BUY. This closes the executable QA #479 family where pre-trade approval previously authorized an order that the same risk engine rejected immediately after deterministic fill costs were applied.

## Persistence and recovery

Trader-owned durable paper state uses the canonical Nika `SQLiteStore`, not a second database. A small trader-owned schema ledger creates simulated fill and account-state tables without taking a shared global migration number while other active lanes own the global schema chain.

A committed fill row and its resulting account snapshot commit in one SQLite transaction. `fill_id` is the durable deduplication key: after process restart, replaying the same already-committed fill returns duplicate/no-op rather than applying it again. A crash before the transaction commits leaves neither fill nor account state.

Current limit: Batch 2 stores the resulting account snapshot as deterministic Decimal-string JSON evidence. Full durable session/order reconstruction is reserved for the paper-session Batch 4 unless earlier Batch 2 CI/audit proves it is required for fill/account correctness.

## Numerical oracle set

`tests/fixtures/trading_research_numerical_oracles.json` contains exactly 42 unique manually calculable cases:

- 10 fee cases;
- 10 slippage cases;
- 12 accounting cases;
- 10 risk cases.

The accounting cases cover long/short open, add, partial close, full close, reversal, fees, weighted basis, realized P&L and equity. The risk cases cover allowed orders, short rejection/allowance, absolute position, gross/net exposure, pending-order exposure, session-loss and drawdown gates.

Focused reservation regressions additionally cover deterministic fee-induced leverage rejection, exact leverage boundary acceptance, cash reservation, fail-closed aggregate pending costs, exact pending-order costs, partial-order remaining quantity and once-per-order fixed fees across multiple fills.

## REUSE / ADAPT / CUSTOM

- REUSE canonical `SQLiteStore`, `ExecutionPolicy`, `fee_for`, `apply_slippage`, `RiskApprovedOrder`, `AccountSnapshot` and Python `Decimal`/datetime primitives.
- ADAPT the integrated Batch 1 market/causality contracts and the existing canonical RiskEngine so deterministic accepted-pending and candidate economics are projected before approval.
- CUSTOM thin: `PendingRiskOrder` and small deterministic reservation helpers. No second risk/accounting engine and no new dependency.

No pandas, NumPy, scikit-learn, Gymnasium, pyarrow, LEAN, Nautilus, Zipline, vectorbt, backtesting.py, QuantStats or broker SDK is added by this batch.

## Evidence truth

Implementation on the branch is not GREEN until Ruff, compile/import, focused 42-oracle/replay/risk/recovery/reservation tests, broad repository tests and exact-candidate CI complete successfully. Independent QA #479 must then be replayed against the repaired exact parent; its source remains QA_ONLY / DO_NOT_MERGE. HUMAN_TESTED=false. NVDA_VERIFIED=false.
