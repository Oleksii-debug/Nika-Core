# AI Trader Research Lab — DEV26 held-out evaluation and metrics

Starting live main: `e40691a6e2ff9c31fd413f63d004612e048d95ed`.
Lane: `MANUAL-DEV26`.
Branch: `work/manual-dev26/heldout-evaluation-metrics`.

## Ownership and compatibility decision

Manual Trader PR #67 is active and owns replay execution, fill math, accounting, risk,
SQLite simulated fill/account persistence, and the shared `trading_research/__init__.py`
export surface.
DEV26 therefore does not edit or import unmerged PR #67 production code. This batch adds only
new held-out-evaluation/metrics modules, focused tests, and this document on top of integrated
Trader Batch 1.

Paper-session reconstruction, pending-order recovery, and integration of metrics with the PR #67
risk/accounting state remain a later compatibility batch after the active owner integrates or
releases those paths. No real-money route is introduced.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE integrated Batch 1 `Partition`, timezone normalization, dataset semantic identity, and
  duplicate/conflict/source-sequence-gap `ValidationReport`.
- REUSE Python `decimal.Decimal`; metrics execute inside an explicit precision-34,
  ROUND_HALF_EVEN local decimal context so caller-modified global precision cannot change results.
- ADAPT Batch 1 validation evidence into strict held-out data-quality evidence. Dirty datasets do
  not silently enter candidate selection or test evidence.
- CUSTOM(thin) the Nika-owned safety semantics: fixed chronological train/validation/test windows,
  validation-only candidate selection, deterministic tie-breaking, fixed-universe identity/cutoff,
  held-out binding, and promotion-metric fail-closed behavior.
- No pandas/NumPy/scikit-learn/Gymnasium/QuantStats/Empyrical dependency is added. Current generic
  analytics packages would add a materially larger float/pandas/scipy/plotting surface for a small
  deterministic Decimal kernel. They remain candidates for non-authoritative analysis/reporting
  only when a measured requirement justifies them.

## Metric conventions

`metrics.py` operates on strictly time-increasing timezone-aware equity observations. Timestamps
are normalized to UTC by the integrated contract. Initial equity must be positive. Zero equity is
allowed as a terminal wipeout; later recovery from zero fails closed because a subsequent simple
return would require division by zero.

The recorded metric assumptions are explicit:

- simple period return: `equity[t] / equity[t-1] - 1`;
- total return: `final_equity / initial_equity - 1`;
- max drawdown: maximum peak-to-current fractional equity loss;
- Sharpe: arithmetic mean of period excess returns divided by sample standard deviation, then
  multiplied by `sqrt(periods_per_year)`;
- Sortino: arithmetic mean above the per-period minimum acceptable return divided by downside
  deviation `sqrt(sum(min(return - MAR, 0)^2) / n)`, then annualized by the same square-root rule;
- risk-free rate and minimum acceptable return are per-period inputs, not silently converted from
  annual rates;
- fewer than two returns, zero volatility, no downside, and no-trade runs return typed
  unavailable reasons instead of infinity, NaN, or fabricated zero ratios.

`PerformanceMetrics` records the annualization/rate assumptions with the result.

## Held-out / leakage rules

A `HeldOutProtocol` fixes non-overlapping train, validation, and test windows and has a
deterministic fingerprint. Candidate selection:

1. consumes validation scores only;
2. requires a single dataset semantic hash and metric definition;
3. requires clean duplicate/conflict/gap evidence;
4. requires candidate fitting to end no later than the train boundary;
5. requires one fixed universe fingerprint and construction cutoff across every candidate; the
   cutoff must precede validation, preventing a future/survivor universe from entering evidence;
6. requires the validation metric to be finalized after the validation window and before selection;
7. requires selection to finish no later than held-out test start;
8. uses deterministic lexical strategy ID tie-breaking and records metric direction.

Held-out binding accepts only the selected strategy on the test partition, on the same dataset,
metric, data-quality evidence, fixed universe fingerprint, and exact universe cutoff. Its fit cutoff
cannot exceed test start, and the test metric cannot be finalized before the test window ends.
Missing held-out metric evidence cannot be converted into promotion evidence.

These contracts produce evaluation evidence only. They contain no order execution, broker,
network, account-funding, or real-money authority, and do not weaken the risk-approved execution
boundary owned by active PR #67.

## Adversarial mathematical evidence

Focused deterministic tests cover:

- exact Decimal return/total-return/drawdown oracles;
- exact Sharpe = 2 and Sortino = 1.5 hand-calculable sequences;
- no-trade, zero-volatility, no-downside, invalid annualization and non-finite inputs;
- 100% wipeout and divide-by-zero recovery rejection;
- timezone-equivalent duplicate instants and out-of-order equity observations;
- hostile caller Decimal precision without metric drift;
- overlapping held-out partitions;
- validation-only selection and deterministic tie-breaking;
- future-fit, future-universe/survivorship, universe substitution, premature validation/test
  evidence;
- dirty duplicate/conflict/gap data rejection;
- dataset/strategy/metric identity mismatch;
- missing held-out metric rejection for promotion evidence.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`. This backend evidence is not a packaged Product
Journey or real-money capability.
