# Product Factory Crash/Restart/Concurrency Acceptance Matrix

Status: **QA_ONLY / production read-only**  
Matrix ID: `NIKA-PF-CRASH-RESTART-CONCURRENCY-V1`

This document defines the long-term independent restart acceptance protocol for Product Factory.
The machine-readable authority for matrix coverage is
`qa/product_factory_restart_acceptance_matrix.json`.

## Acceptance rule

A boundary receives PASS credit only from terminal executable evidence on the exact candidate SHA
being classified. An open owner PR, historical green SHA, owner-authored assertion, model-only
simulation, or compatible-looking predecessor is not PASS evidence.

ONE-SHOT-57 does not patch feature production. A RED is routed to the canonical owner PR/lane.
The QA lane may add or strengthen independent oracles without changing feature semantics.

## Required fault injection

Every listed Product Factory boundary must be exercised against all eight modes:

1. effect before state;
2. state before effect;
3. lost acknowledgement;
4. duplicate retry;
5. concurrent writer;
6. stale checkpoint;
7. restart with corrupt durable type/version;
8. two process-like recoverers.

The Cartesian product is therefore exactly `14 boundaries * 8 modes = 112 cells`.

Every cell must assert all five invariants:

- `NO_DUPLICATE_EFFECT`;
- `NO_LOST_STATE`;
- `NO_STALE_AUTHORITY`;
- `NO_CROSS_PROJECT_DATA`;
- `ONE_CANONICAL_OWNER`.

## Deterministic execution protocol

For each cell, the oracle must use an explicit synchronization/fault point rather than timing-based
sleep races. The sequence is:

1. construct two distinct ProductProject identities plus the exact target operation identity;
2. establish canonical durable pre-state and record its revision/checkpoint/receipt identity;
3. arm the named fault point for the selected mode;
4. execute until the deterministic barrier or injected termination;
5. destroy the in-memory host objects;
6. construct a fresh host from the same durable store;
7. when the mode requires concurrency, construct a second independent host and release both from
   the same barrier;
8. reconcile or retry only through the production public boundary being audited;
9. assert effect count, durable state, authority lineage, project isolation, and canonical owner;
10. restart once more and repeat the terminal assertions to detect acknowledgement-only success.

A two-recoverer oracle is not satisfied by two calls through the same in-process lock. It must use
independent host instances and, where the production claim is cross-process, process-like or actual
cross-process ownership evidence.

## Current baseline routing

The baseline captured by this QA matrix started from live main
`23c7c1ce97b263b4aafa61bdcbace207b4476a3d`.

Primary owner routes are recorded in the JSON matrix. Important explicit blockers at this baseline:

- team create/replace: PR #163 states full durable `TeamLifecycleSnapshot` wiring is not integrated;
- credential handle: PR #162 does not prove cross-process atomicity beyond its single authority host;
- deployment: PR #280 states pre-dispatch `UNCERTAIN` is not durably committed to canonical SQLite
  before the provider effect;
- health/PF8 maintenance: PR #286 states the provider-effect-before-durable-host-save process-crash
  window is not closed.

Backup/update preserves one recovery owner: `SQLiteRecoveryManager` under PR #311. Release recovery
PR #218 is only a thin adapter and must never become a second SQLite recovery authority.

## Validator

Run the structural oracle with:

```text
python -m pytest -q tests/test_product_factory_restart_acceptance_matrix.py
```

The validator intentionally checks matrix completeness and evidence truth; it does not claim that a
structurally present cell has passed production fault injection. Boundary-specific executable QA
oracles remain independent evidence and must be linked to an exact candidate SHA before PASS.

## Human evidence truth

This QA matrix is non-UI durability/security evidence. It does not grant `HUMAN_TESTED` or
`NVDA_VERIFIED`; both remain false unless separately proven by the required human Windows/NVDA
acceptance process.
