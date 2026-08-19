# Agent Lab evaluation semantics repair

Date: 2026-08-19.
Lane: AUTO03 generic Agent Lab / controlled experiments.
Base main: `9ade4ed749a2b4a25ac11a35435f9b306caefb55`.

## Defects closed

1. M8 treated every primary metric as higher-is-better. A latency, cost, loss or error-rate experiment could therefore reject a genuinely better lower-valued challenger and could prefer a numerically worse candidate.
2. M7 evaluator aggregation grouped only by target member and silently averaged records carrying different metric names. Unlike quantities such as quality and latency could therefore collapse into one meaningless score.
3. `EvaluationScore` accepted NaN and infinity, allowing non-finite evidence to poison deterministic aggregation.

## Contract repair

`PromotionPolicy` now carries `primary_higher_is_better`, defaulting to `True` for compatibility. Promotion improvement is normalized into the declared beneficial direction before applying `minimum_improvement`, and eligible challenger ranking uses that same direction.

The direction is persisted in the existing M8 immutable definition JSON. No SQLite schema migration is needed. Definitions persisted before the field existed decode as higher-is-better, preserving historical behavior and allowing their lifecycle to continue.

M7 evaluator aggregation remains a simple deterministic mean per target member, but one call may contain only one metric. Mixed metric sets fail closed. Evaluation scores must be finite.

## Reuse decision

CUSTOM (thin). These are Nika-owned experiment/evaluator semantics already identified as custom product policy in the reuse catalog. No new dependency, framework or orchestration kernel is introduced.

## Regression evidence added

`tests/test_agent_lab_evaluation_semantics.py` covers:
- lower-is-better challenger promotion;
- lower-is-better minimum-improvement rejection;
- SQLite persistence of primary metric direction;
- compatibility with legacy persisted definitions lacking the new field;
- mixed-metric evaluator rejection;
- NaN/+Inf/-Inf evaluator rejection.

Existing higher-is-better M8 and same-metric M7 tests remain the backward-compatibility baseline.

## Evidence boundary

This document records IMPLEMENTED source intent only. GREEN requires exact-head repository CI, and INTEGRATED requires merge of that exact green candidate. `HUMAN_TESTED` and `NVDA_VERIFIED` are unrelated and remain false unless the designated human protocol is actually executed.
