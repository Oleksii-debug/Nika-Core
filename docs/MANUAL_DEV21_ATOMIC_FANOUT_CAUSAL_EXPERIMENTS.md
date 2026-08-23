# MANUAL-DEV21 — atomic fan-out, durable cancellation and causally safe experiments

Starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
Compatibility main: `e40691a6e2ff9c31fd413f63d004612e048d95ed`.
Lane: `work/manual-dev21/atomic-fanout-causal-experiments`.

## Scope

This batch hardens the already integrated M7/M8 foundations. It does not introduce a second
orchestration kernel, a new model gateway, an autonomous source-rewriter, or a new permission
system.

### M7: atomic fan-out admission

The prior durable-team lifecycle made each individual child restart-safe, but a fan-out wave could
still be partially admitted. `MultiAgentStore.spawn_children()` is now the canonical admission
primitive:

- `BEGIN IMMEDIATE` obtains one SQLite writer boundary before quota decisions;
- depth, remaining children-per-parent quota, and remaining total-agent quota are evaluated for the
  whole requested wave;
- privilege attenuation and durable thread-identity uniqueness are validated before the first child
  insert;
- every child identity, TASK handoff, and `multi_agent.child_spawned` audit event is committed in
  the same transaction;
- any late database/handoff constraint failure rolls back the entire wave;
- competing fan-out waves serialize at the SQLite writer boundary;
- legacy `spawn_child()` remains a one-child wrapper over the atomic primitive.

Runtime execution begins only after the complete wave is durably admitted.

### M7: cancellation authority and uncertain-effect reconciliation

AUD03 proved that the previous supervisor called every external `runtime.cancel()` before committing
the team's durable cancellation state. An external effect followed by an exception could therefore
leave durable authority `ACTIVE`; restart or retry could recover or cancel the same work again.

The repaired flow is state-first and effect-aware:

1. a versioned M7 cancellation extension transaction records one stable operation and every exact
   member/task/thread effect identity;
2. that same transaction changes the team and every nonterminal member to `CANCELLED` before the
   first external cancellation call;
3. each external call must first persist `DISPATCHING`;
4. normal adapter return records `CONFIRMED`;
5. an exception records `RECONCILE_REQUIRED`; a crash after `DISPATCHING` is also treated as
   uncertain after restart and is never blindly replayed;
6. confirmed effects are skipped on retry;
7. uncertain effects can move forward only through the optional read-only
   `CancellationReconciliationPort`, whose exact verdict is `CANCELLED`, `NOT_CANCELLED`, or
   `UNKNOWN`;
8. `NOT_CANCELLED` permits exactly that effect to return to `PLANNED`; `CANCELLED` records it as
   confirmed; `UNKNOWN` remains blocked;
9. team recovery, new fan-out, and team finalization fail closed while a durable cancellation
   operation is unfinished.

The external cleanup journal is stored in the same canonical SQLite database. It uses an additive
M7-owned schema version stream instead of taking the shared global migration number currently used
by independent research lanes. Future M7 cancellation schema versions fail closed.

### M8: immutable evaluation-data provenance and causal cutoff

The existing M8 engine already requires an explicit primary metric, fixed versioned replay cases,
complete replay coverage, deterministic promotion, permission-fingerprint equality, durable events,
and rollback. This batch adds a generic data-leakage boundary without forcing scikit-learn, DSPy,
or Gymnasium into the baseline engine.

New backward-compatible immutable definition metadata:

- `DatasetSplit`: `training`, `evaluation`, or `held_out`;
- `StrategyRef.training_dataset_fingerprints` for exact known training-data identities;
- `ReplayCase.dataset_fingerprint` for exact evaluation-data identity;
- `ReplayCase.data_end_at` for timezone-aware latest data time;
- `ExperimentDefinition.evaluation_cutoff` for the causal cutoff.

Fail-closed rules apply when the corresponding provenance is declared: training splits cannot be
promotion evidence; known training provenance requires evaluation fingerprints; overlapping
training/evaluation identity is rejected; future data beyond a declared cutoff is rejected; missing
or timezone-naive temporal evidence is rejected; and fingerprint identities must be canonical
without leading/trailing whitespace.

These fields persist inside the existing immutable experiment-definition JSON. No M8 SQLite schema
migration is needed. A replay without explicit split provenance defaults conservatively to
`evaluation`, never `held_out`. Legacy persisted definitions that predate the split field therefore
retain their promotion compatibility without being retroactively upgraded to held-out evidence.
Persisted definition decoding also fails closed on wrong JSON scalar/container types instead of
coercing strings, numbers, or booleans into plausible experiment policy/provenance values.

## REUSE → ADAPT → CUSTOM (thin)

- **REUSE:** `AgentRuntimePort`, LangGraph only behind that port, canonical SQLite, `ToolGrant`
  attenuation, M8 repository/event/promotion lifecycle, and generic `AuditLog` public append API.
- **ADAPT:** the existing team store into whole-wave admission; the existing team cancellation path
  into state-first durable cleanup; immutable M8 definition JSON into a causal-data evidence
  carrier.
- **CUSTOM (thin):** Nika aggregate quota, durable cancellation operation/effect identity,
  reconciliation policy, and generic dataset contamination/cutoff invariants.
- **Not added:** scikit-learn, DSPy, Gymnasium, a second runtime, a training framework, or a
  production-source mutation API.

## Verification matrix

Focused deterministic regressions cover atomic remaining-quota admission, transaction rollback,
concurrent fan-out, durable thread identity, exact AUD03 cancellation effect→exception, restart with
no blind replay, crash after `DISPATCHING`, concurrent cancel callers, cancellation intent rollback
before external effect, reconciliation verdicts, future extension-schema rejection, contaminated or
future evaluation data, valid held-out promotion, SQLite restart, conservative legacy split
decoding, and fail-closed persisted-type corruption.

Acceptance credit requires exact-head Ruff, compile, complete pytest, and GitHub Ubuntu/Windows CI.
Automated tests do not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
