# AI Trader DEV26 — held-out evidence integrity hardening

Lane: `MANUAL-DEV26`.
Compatibility baseline for this repair: `e8743566ffc673d6f8d272e88de0e027c23ab277`.

This dossier records the adversarial integrity boundaries that promotion evidence must survive.
No repair adds execution, broker, account, network, or real-money authority.

## 1. Factory-only promotion authority

`SelectionDecision` can only be produced by `select_validation_candidate()`.
`HeldOutAssessment` can only be produced by `bind_held_out_test()`.

Public raw score/result records are revalidated at each authority boundary. Canonical identity
fields reject whitespace variants, malformed SHA-256 values, non-Decimal metrics, non-finite
metrics, malformed timestamps, and invalid enum/policy values.

## 2. Exact quality and metric identity

Data quality is not represented only by issue counts. `ReplayDataQuality.from_report()` hashes the
exact duplicate/conflict/gap issue evidence. Selection and held-out binding require equality of the
counts and evidence hash in addition to the dataset semantic hash.

Metric identity is a canonical name plus an exact metric-definition SHA-256. A same-named metric
with changed sampling/rate/formula assumptions cannot be substituted by keeping only the display
name.

## 3. Immutable strategy artifact

A selected strategy is not merely `strategy_id`. The selected fitted artifact is sealed by
version, algorithm/configuration/feature-pipeline/fitted-state digests, deterministic seed, fit
cutoff, and creation time.

Each raw candidate/result also records the artifact fingerprint established at construction.
Changing the nested artifact later is rejected before selection/binding.

## 4. Protocol and refit authority

The protocol fingerprint includes train/validation/test windows and `RefitPolicy`.

`NO_REFIT` requires the exact validation-selected fitted artifact in held-out evaluation.

`REFIT_TRAIN_VALIDATION` permits only a post-selection, pre-test artifact with unchanged strategy
definition and fit cutoff exactly equal to validation end. A fit cutoff after validation end is
treated as future/test leakage.

## 5. Post-construction mutation cannot rewrite promotion chronology

Held-out binding reconstructs validated snapshots of the protocol, selection, result, strategy
artifact, and quality evidence. The assessment stores an authority fingerprint over those snapshots.

Therefore:

- mutating the original selection/result/protocol after binding does not alter the assessment;
- mutating `selected_at`, held-out `evaluated_at`, metric value, metric identity, universe, quality,
  protocol, or strategy artifact inside the bound snapshots invalidates the authority seal;
- `require_promotion_metric()` revalidates the full identity and chronology before returning a
  Decimal;
- unavailable held-out metrics are never promotion evidence.

This closes the earlier class where a valid object could be mutated after construction/binding and
then reused as if its original chronology still applied.

## 6. Sampling-grid attack

A caller cannot claim `periods_per_year` independently of timestamps. Regular annualization
requires a sealed sampling spec and exact cadence. Irregular observations return typed annualization
unavailability rather than an apparently valid annualized ratio.

## 7. No execution authority

The DEV26 production modules import only standard-library numerical/identity utilities and existing
Trader contracts/dataset evidence. The focused structural test rejects network/broker import
surfaces. There is no order placement, funding, broker SDK, account mutation, risk bypass, or
real-money path.

PR #67 retains ownership of execution/accounting/risk source and is intentionally untouched.

## Evidence truth

Focused reconstructed DEV26 evidence before hosted CI: `32/32 PASS`, plus compile success.
Exact-head GitHub Core and M12 are authoritative. Independent AUD03 exact-head replay is required
before DEV26 may be described as audited/integration-ready.

`REAL_MONEY_AUTHORITY=false`.
`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
`PRODUCTION_RELEASE_READY=false`.
