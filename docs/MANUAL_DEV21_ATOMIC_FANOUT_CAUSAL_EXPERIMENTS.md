# MANUAL-DEV21 — atomic fan-out and causally safe experiments

Starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
Lane: `work/manual-dev21/atomic-fanout-causal-experiments`.

## Scope

This batch hardens the already integrated M7/M8 foundations. It does not introduce a second
orchestration kernel, a new model gateway, an autonomous source-rewriter, or a new permission
system.

### M7: atomic fan-out admission

The prior durable-team lifecycle made each individual child restart-safe, but a fan-out wave could
still be partially admitted: `MultiAgentSupervisor.fan_out()` called `spawn_child()` repeatedly, so
an already-partially-consumed parent/team quota could allow early children and reject a later child.
A restart would then observe an unintended partial wave.

`MultiAgentStore.spawn_children()` is now the canonical fan-out admission primitive:

- `BEGIN IMMEDIATE` obtains one SQLite writer boundary before quota decisions;
- depth, remaining children-per-parent quota, and remaining total-agent quota are evaluated for the
  whole requested wave;
- privilege attenuation is validated before the first child insert;
- every child identity, TASK handoff, and `multi_agent.child_spawned` audit event is committed in the
  same transaction;
- any late database/handoff constraint failure rolls back the entire wave;
- competing fan-out waves serialize at the SQLite writer boundary, so both cannot consume the same
  remaining aggregate quota;
- legacy `spawn_child()` remains as a one-child wrapper over the atomic primitive.

The supervisor still validates activated agent definitions and definition-level grants before
admission, and obtains durable runtime initial-resume tokens before the store transaction. Runtime
execution begins only after the complete wave is durably admitted.

### M8: immutable evaluation-data provenance and causal cutoff

The existing M8 engine already requires an explicit primary metric, fixed versioned replay cases,
complete replay coverage, deterministic promotion, permission-fingerprint equality, durable events,
and rollback. This batch adds a generic data-leakage boundary without forcing scikit-learn, DSPy,
or Gymnasium into the baseline engine.

New backward-compatible immutable definition metadata:

- `DatasetSplit`: `training`, `evaluation`, or `held_out`;
- `StrategyRef.training_dataset_fingerprints`: exact content identities of data used to produce a
  strategy when that provenance is known;
- `ReplayCase.dataset_fingerprint`: exact content identity of evaluation data;
- `ReplayCase.data_end_at`: timezone-aware latest data timestamp for temporal experiments;
- `ExperimentDefinition.evaluation_cutoff`: timezone-aware causal cutoff.

Fail-closed rules are activated when the corresponding provenance is declared:

1. a promotion replay may never use `DatasetSplit.TRAINING`;
2. when any candidate declares training-data fingerprints, every promotion replay must declare its
   own fingerprint;
3. promotion data whose fingerprint overlaps any candidate training fingerprint is rejected before
   experiment creation;
4. when an evaluation cutoff is declared, every replay must declare `data_end_at` and it must be at
   or before the cutoff;
5. naive datetimes are rejected so comparisons cannot depend on host-local timezone assumptions.

These fields persist inside the existing immutable experiment-definition JSON. No SQLite schema
migration is needed. Legacy persisted definitions decode as held-out replays with no fingerprint or
cutoff, preserving historical M8 behavior until a caller opts into stronger provenance evidence.

## REUSE → ADAPT → CUSTOM (thin)

- **REUSE:** existing `AgentRuntimePort`, existing LangGraph adapter only behind that port, canonical
  SQLite transactions, existing `ToolGrant` attenuation, existing M8 repository/events/promotion
  lifecycle.
- **ADAPT:** the existing team store into a whole-wave transactional admission boundary; existing
  immutable M8 definition JSON into a causal-data evidence carrier.
- **CUSTOM (thin):** Nika-specific aggregate quota admission and generic dataset
  provenance/contamination/cutoff invariants.
- **Not added:** scikit-learn, DSPy, Gymnasium, a new orchestration runtime, a training framework, or
  any production-source mutation API.

## Verification matrix

Focused deterministic regressions cover:

- parent remaining quota rejects the entire fan-out wave and remains absent after restart;
- total-agent remaining quota rejects the entire wave;
- a late handoff uniqueness failure rolls back every child in the transaction;
- two concurrent batches cannot overbook the same total-agent capacity;
- training/evaluation dataset overlap is rejected;
- declared training provenance without evaluation fingerprints fails closed;
- training split cannot be promotion evidence;
- future data beyond the causal cutoff is rejected;
- declared cutoff without replay temporal provenance fails closed;
- timezone-naive temporal identities are rejected;
- valid held-out data can still promote deterministically;
- dataset provenance/cutoff survive SQLite restart and promotion continues;
- legacy persisted definitions decode with backward-compatible defaults.

Acceptance credit requires exact-head Ruff, compile, complete pytest, and GitHub Ubuntu/Windows CI.
Automated tests do not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
