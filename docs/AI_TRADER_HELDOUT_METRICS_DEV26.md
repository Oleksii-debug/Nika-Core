# AI Trader Research Lab — DEV26 held-out evaluation and metrics

Lane: `MANUAL-DEV26`.
Branch: `work/manual-dev26/heldout-evaluation-metrics`.
Compatibility baseline for this repair: live `main` `e8743566ffc673d6f8d272e88de0e027c23ab277`.

## Ownership boundary

PR #67 remains the active owner of deterministic replay execution, fill math, accounting, risk,
simulated fill/account persistence, strategy execution, and the shared
`trading_research/__init__.py` export surface. DEV26 does not edit those paths and does not import
unmerged #67 implementation.

DEV26 is an evaluation-only slice. It contains no broker adapter, network transport, order
submission, account funding, real-money route, or risk bypass.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE integrated Batch 1 `Partition`, UTC normalization, `DatasetVersion.semantic_hash`, and
  `ValidationReport`.
- REUSE Python `decimal.Decimal`, `datetime`, `hashlib`, and `itertools`.
- ADAPT `ValidationReport` into an exact quality-evidence fingerprint rather than retaining only
  issue counts.
- CUSTOM(thin) only the Nika-specific evaluation authority: sampling proof, immutable strategy
  artifact identity, train/validation/test protocol, validation-only selection, refit policy,
  fixed-universe identity, held-out binding, and promotion-evidence sealing.
- No pandas, NumPy, scikit-learn, Gymnasium, QuantStats, or Empyrical dependency is added. These
  remain optional analysis candidates; they do not replace the authoritative Decimal kernel.

## Decimal metric conventions

`metrics.py` uses a precision-34 `ROUND_HALF_EVEN` local Decimal context. Caller changes to the
global Decimal context do not alter the result.

The formulas are:

- simple return: `equity[t] / equity[t-1] - 1`;
- total return: `final_equity / initial_equity - 1`;
- max drawdown: maximum peak-to-current fractional equity loss;
- Sharpe numerator: arithmetic mean of per-period returns minus the per-period risk-free rate;
- Sharpe denominator: sample standard deviation (`n - 1`);
- Sortino numerator: arithmetic mean of return minus the per-period minimum acceptable return;
- Sortino downside deviation:
  `sqrt(sum(min(return - MAR, 0)^2) / n)`;
- annualization for both ratios: multiply by `sqrt(periods_per_year)`.

Risk-free rate and MAR are per-observation-period values. They are not silently converted from
annual rates.

## Sampling authority

Annualized Sharpe/Sortino are available only for `SamplingMode.REGULAR` with a sealed
`SamplingSpec` containing:

- canonical calendar identity;
- exact positive cadence;
- finite positive Decimal `periods_per_year`;
- `MissingPeriodPolicy.REJECT`.

Every consecutive equity timestamp must match the declared cadence exactly. A missing or irregular
period fails closed. `SamplingMode.IRREGULAR` may still produce total return and drawdown, but
annualized Sharpe/Sortino return the typed
`RatioUnavailableReason.ANNUALIZATION_UNAVAILABLE`; arbitrary irregular observations are never
silently annualized.

The result records the sampling fingerprint, calendar, cadence, annualization count, risk-free
rate, and MAR. Mutation of the sampling evidence after construction is rejected.

## Typed edge cases

Ratios never fabricate `0`, `Infinity`, or `NaN` for unavailable cases. Typed reasons cover:

- no trades;
- no returns;
- insufficient returns;
- zero volatility;
- no downside;
- unavailable annualization.

Equity and metric inputs must be finite Decimals. Initial equity must be positive. Zero equity is a
terminal wipeout, giving max drawdown `1`; recovery after zero is rejected because the next simple
return would divide by zero. Malformed timestamps, sampling values, counts, rates, metric values,
and digests fail closed.

## Held-out protocol and exact identity

`HeldOutProtocol` requires non-overlapping train, validation, and test windows. Its fingerprint
includes the refit policy.

Candidate selection is validation-only and requires every candidate to share exact:

- metric name and metric-definition SHA-256;
- dataset semantic SHA-256;
- complete data-quality evidence fingerprint and counts;
- fixed universe SHA-256;
- universe construction cutoff.

The universe cutoff must be strictly before validation. Candidate fitted artifacts must have fit
cutoff no later than train end and must exist before validation begins. Validation metrics must be
finalized after validation ends and before selection. Selection must complete no later than held-out
test start. Equal metrics tie-break lexically by strategy ID.

## Immutable strategy artifact

Selection binds a `StrategyArtifactFingerprint` containing:

- strategy ID and version;
- algorithm SHA-256;
- configuration SHA-256;
- feature-pipeline SHA-256;
- fitted-state SHA-256;
- deterministic seed;
- fit cutoff;
- artifact creation time.

The held-out result must preserve the selected strategy definition. A same-named strategy with a
different version/configuration/feature pipeline/algorithm/seed is not the selected artifact.

## Sealed refit policy

Two explicit policies exist:

- `NO_REFIT`: held-out evaluation must use the exact selected fitted-artifact fingerprint.
- `REFIT_TRAIN_VALIDATION`: strategy definition remains unchanged; fit cutoff must equal validation
  end; the refit artifact must be created no earlier than selection and no later than held-out test
  start.

Any fit cutoff after validation end is test/future leakage and is rejected.

## Held-out promotion evidence

A held-out assessment requires a test-partition result for the selected strategy with exact metric,
dataset, quality, universe, cutoff, and strategy/refit identity. The held-out metric cannot be
finalized before test end.

`HeldOutAssessment` snapshots protocol, selection, and result, then seals their authority
fingerprint. Later mutation of the original evidence cannot rewrite the assessment. Low-level
mutation of the bound snapshots invalidates the seal. A missing held-out metric cannot become
promotion evidence.

This module does not convert Decimal metrics to the current M8 float metric surface. Any future
M8 integration needs an explicit compatibility decision rather than an uncontrolled Decimal-to-float
promotion path.

## Local repair evidence

Before the GitHub exact-head gate, the reconstructed focused DEV26 runner passed `32/32` tests plus
`compileall`. The authoritative acceptance evidence remains exact-head Core CI and M12 on GitHub,
followed by independent AUD03 replay.

`REAL_MONEY_AUTHORITY=false`.
`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
`PRODUCTION_RELEASE_READY=false`.
