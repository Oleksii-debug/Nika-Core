# AI Trader Research Lab — Batch 2 replay, accounting, risk

Starting integrated main: `782105eba05da9714c30d45483049dbb1fe06370`.
Current-main compatibility refresh base: `ce332821607c964371741989363938965e7162a6`.

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

## Decimal accounting

Authoritative simulated money and position calculations use `decimal.Decimal`: cash, fees, signed quantity, average basis, realized P&L, unrealized P&L, equity, gross exposure and net exposure. Long and short adds, reductions, closes and reversals use deterministic average-cost accounting.

## Risk

`RiskEngine` evaluates the current position plus pending signed quantity plus the candidate intent before creating `RiskApprovedOrder`. Limits cover absolute position, gross exposure, net exposure, session loss, drawdown, short policy and leverage. Post-fill assertions recheck position, exposure, leverage and loss/drawdown boundaries.

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

## REUSE / ADAPT / CUSTOM

- REUSE canonical `SQLiteStore` and Python `Decimal`/datetime primitives.
- ADAPT the integrated Batch 1 `Instrument`, market-event, `TemporalView`, deterministic event ordering and strategy context contracts.
- CUSTOM thin deterministic replay/accounting/risk policy because the binding causal phase order, no-same-slice fill invariant, typed risk approval boundary and exactly-once simulated ledger semantics are Nika product contracts rather than a third-party engine contract.

No pandas, NumPy, scikit-learn, Gymnasium, pyarrow, LEAN, Nautilus, Zipline, vectorbt, backtesting.py, QuantStats or broker SDK is added by this batch.

## Evidence truth

Implementation on the branch is not GREEN until Ruff, compile/import, focused 42-oracle/replay/risk/recovery tests, broad repository tests and exact-candidate CI complete successfully. HUMAN_TESTED=false. NVDA_VERIFIED=false.
