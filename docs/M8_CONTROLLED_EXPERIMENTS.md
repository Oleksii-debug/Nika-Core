# M8 Controlled self-learning and experiment engine

Status: full M8 candidate on `dev-b/m8-controlled-experiments`; no product-weight credit until exact Ubuntu + Windows acceptance evidence is green and the PR is integrated.

## Reuse decision

- REUSE the already integrated Nika runtime/model/memory/scheduler/resource contracts; M8 does not add another orchestration or model gateway.
- REUSE Python deterministic arithmetic/statistics and canonical SQLite transactions for baseline evaluation and durable evidence.
- ADAPT DSPy only for later experiments with an explicit dataset and metric where its optimizer/evaluator model materially reduces optimization glue. It is not required for deterministic champion/challenger bookkeeping.
- ADAPT Gymnasium only for controlled simulation/RL environments that actually need its reset/step environment contract; it is not a mandatory M8 dependency.
- CUSTOM (thin): immutable experiment identity, replay evidence, permission-fingerprint equality, promotion/guardrail policy, rollback evidence and the Nika repository port because these are product/safety semantics.

No new broad learning dependency is added merely to claim self-learning. The baseline engine is deterministic and dependency-light.

## Safety boundary

Experiment candidates are restricted to versioned prompt/strategy/config artifacts. There is no production-source mutation API in M8. Every challenger must carry the same permission fingerprint as the champion; a candidate that changes the permission boundary is rejected before the experiment can exist.

M8 does not authorize tool calls, external writes, financial actions, legal actions or destructive actions. Existing R4 execution-time approval remains authoritative. Trading/gambling learning remains backtest/paper/demo/simulation-only unless a separate future R4 connector and human gate are explicitly approved.

## Deterministic evaluation

Each observation is bound to candidate + declared replay + declared metric. Duplicate observations for the same candidate/replay/metric fail closed. Metric values must be finite. Completion requires full replay coverage for the primary metric and every guardrail for every candidate.

The primary metric declares its optimization direction. `primary_higher_is_better=True` preserves the original quality/accuracy behavior; `False` supports latency, cost, loss, error-rate and other metrics where a lower value is better. `minimum_improvement` is interpreted in the beneficial direction, so the configured threshold always means “at least this much better than champion” rather than assuming numeric increase.

Promotion requires:
1. the challenger meets the configured minimum primary-metric improvement over the champion in the declared beneficial direction;
2. every guardrail stays within its maximum permitted regression;
3. the fixed replay set is fully covered.

If multiple challengers qualify, selection is deterministic by beneficial primary score and then candidate ID. If none qualifies, the champion remains selected and the experiment completes without promotion. Rollback is legal only after a recorded promotion and restores the recorded previous champion identity.

## Durable SQLite persistence

Schema migration v7 adds three authoritative M8 tables:

- `experiments` stores immutable canonical definition JSON plus current lifecycle/promotion state;
- `experiment_observations` stores append-only candidate/replay/metric evidence with a unique evidence key;
- `experiment_events` stores append-only lifecycle/promotion/rollback evidence.

`SQLiteExperimentRepository` implements the stable `ExperimentRepository` port. Definitions cannot change after creation. Recorded observations cannot be removed or changed. A stale writer that would drop evidence fails closed. Repository saves acquire an immediate SQLite write transaction so current evidence is re-read under the writer lock before append/state mutation.

Primary metric direction is persisted inside the existing immutable definition JSON, so this correctness repair does not require a schema migration. Definitions written before the direction field existed decode as `primary_higher_is_better=True`, preserving the historical M8 behavior and allowing old SQLite experiments to continue through normal lifecycle transitions.

Lifecycle updates and their event evidence are one local transaction. Fault-injection tests deliberately abort event insertion after the experiment status update and verify that SQLite rolls the whole transition back. Restart tests recreate the store/repository/engine from the same database, continue a partially recorded run, promote a challenger, recreate the process again and roll back to the recorded champion.

Allowed persisted lifecycle transitions are deliberately narrow:

`draft -> running -> completed|promoted -> rolled_back (promotion only)`

Illegal direct state jumps fail closed even if a caller bypasses `ExperimentEngine` and calls the repository port directly.

## Acceptance gate

Before M8 receives its 10% roadmap weight:

1. exact candidate passes dependency consistency, Ruff, compile and full pytest;
2. Ubuntu and Windows shared verification are green on the exact same head;
3. schema v7 migration passes old-database upgrade regression tests;
4. durable repository survives process recreation without losing definition, replay evidence or promotion state;
5. fault-injected transition/event failure rolls back atomically;
6. duplicate, unknown and stale-writer evidence paths fail closed;
7. permission-fingerprint drift and definition/evidence mutation are rejected;
8. promotion threshold, metric direction, guardrail denial and rollback tests pass;
9. no M8 path can rewrite production source or silently widen permissions;
10. exact SHA/CI evidence is recorded before integration.

`PACKAGED`, `HUMAN_TESTED` and `NVDA_VERIFIED` are separate later gates and are not claimed by M8.
