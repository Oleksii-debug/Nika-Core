# ONE-SHOT-08 PF5 Build Execution Integrity Evidence — 2026-08-23

## Identity

- Repository: `Oleksii-debug/Nika-Core`.
- Successor starting main: `3fbfabfc93d59183f174ff44098db886cff93bd8`.
- Branch: `work/one-shot-08/pf5-durable-build-execution`.
- Predecessor: PR #177, exact head `f44fa9a64c5641274770e226d61816f8a9da4a9c`.
- Predecessor Core/M12 evidence does not transfer acceptance credit to this successor head.

## Compatibility / ownership decision

This successor carries PR #177's five PF5-owned files onto the current main and adds only PF5 durable
host/persistence/test surfaces. It does not edit:

- DEV27 low-level containment paths;
- shared `product_factory_deployment.py` owned by active PF6 work;
- Product Factory coordinator/checkpoint-host files owned by other active lanes;
- credential or provider implementation files.

Canonical storage is reused through existing `SQLiteStore`, `tasks`, `checkpoints`, and `audit_events`.
No second repository/database/state framework was introduced.

## Repaired acceptance families

The successor retains the predecessor's repaired authority model:

- candidate request has no execution authority and no arbitrary argv;
- trusted host resolves exact project/repository/work authority;
- node/workspace/network/credential/command requests are strict subsets;
- generic shell entrypoints fail closed;
- authority is rechecked before prepare/dispatch/effect and on restart;
- `EFFECT_IN_FLIGHT` precedes real execution;
- uncertain/lost-acknowledgement work is inspection-only;
- source SHA, dispatch, lease/node capability, authority and normalized evidence are restart-validated;
- unavailable platforms wait without fabricated evidence.

The known persistence gap is addressed by `SQLiteBuildExecutionCheckpointStore` and
`DurableBuildExecutionHost`:

- every host mutation is checkpointed when logical state changes;
- the decorated real node port cannot run until exact `EFFECT_IN_FLIGHT` state is durable;
- persistence failure poisons the live host and requires restart/restore;
- durable state is checksum/schema/project/task bound;
- transition sequence cannot skip/regress;
- PF5-owned leases are restored only against the current node registry;
- restart across external effect becomes `RECONCILE_REQUIRED`, never blind replay;
- exact changed-file evidence is bound to current trusted output path/count/case policy.

## New focused adversarial cases

`tests/test_product_factory_build_execution_host.py` adds proof for:

1. SQLite contains exact `EFFECT_IN_FLIGHT` and dispatch identity before the real port observes `run()`;
2. lost acknowledgement invokes real `run()` once and resolves only through `inspect()`;
3. crash after dispatch survives restart as inspection-only reconciliation;
4. restart re-resolves current authority and rejects revoked build permission;
5. changed file outside host output paths cannot become terminal success;
6. Windows case aliases fail closed while Linux case-distinct output remains distinct;
7. unavailable platform state is durably `WAITING_FOR_NODE` with zero execution calls;
8. an existing durable DB requires explicit restore before any new effect;
9. recomputing a tampered checkpoint checksum cannot substitute ProductProject identity.

## Test truth

At authoring time only syntax/AST/line-length preflight has been performed locally because this runtime
cannot reach GitHub from its local container and has no installed repository dependency environment.
No successor Core/M12 GREEN is claimed here until exact GitHub Actions evidence exists.

Required qualification before integration:

- exact successor-head Ruff/compile/focused/full Core CI;
- exact successor-head complete M12 Pre-Human Release Gate;
- independent security/reliability replay of authority, persistence, lost-ack, restart and output-budget
  attacks;
- fresh comparison against current main immediately before guarded integration.

## Truth flags

- `IMPLEMENTED=true` for the successor candidate code.
- `EXACT_HEAD_GREEN=false` until Actions prove it.
- `READY_FOR_INTEGRATION=false` until exact CI + independent audit.
- `INTEGRATED=false`.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- `PRODUCTION_RELEASE_READY=false`.
