# Product Factory checkpoint authority and durable lineage

Status: implementation evidence for the MANUAL-DEV05 Product Factory orchestration-integrity lane.

This document narrows the durability contract for `ProductFactoryCheckpointHost` and `ProductProjectCoordinatorBinding`. It does not redefine PF acceptance gates, ProductProject ownership, repository-graph ownership, worker-adapter ownership, or human/NVDA evidence.

## Trust boundary

A coordinator checkpoint is candidate state. Its serialized bytes, work requests, trusted-plan descriptor, checksums, work IDs, and any hash recomputed from those fields are not independent authority.

The initial trusted-plan fingerprint is admitted only when a live `ProductProjectCoordinatorBinding.checkpoint()` crosses the host-side boundary. `ProductProjectCoordinatorCheckpoint.trusted_plan_fingerprint` is not constructor-controlled and is not serialized. A checkpoint loaded from durable bytes therefore carries no live authority value.

After the first save, the canonical authority is the fingerprint anchored in the Product Factory host-task payload. Every later save and restore validates the candidate snapshot against that durable host-task anchor. A legacy checkpoint that exists without that anchor fails closed and requires explicit reconciliation.

## Durable predecessor requirement

Static validation of one self-consistent snapshot is insufficient for long-running orchestration. For every higher-revision save, the checkpoint host also validates the candidate against the latest durable predecessor for the same host task.

The predecessor contract is:

- project ID, ProductProject spec version, and ProductProject row version do not drift inside one host-task lineage;
- component identity is stable;
- within one attempt, the complete work request is immutable;
- same-attempt work state moves only through an allowed forward transition;
- an attempt may advance by exactly one generation;
- attempt `N + 1` requires attempt `N` to have been durably saved as `repair_required`;
- repair keeps project, component, repository, path scope, permission ceiling, and acceptance commands unchanged;
- repair goal lineage is the previous durable goal plus exactly one non-empty `Repair:` reason;
- accepted and blocked terminal states cannot be recomputed backwards into executable states.

These checks intentionally reject a candidate that performs several security-significant transitions only in memory and then tries to persist the final state as though the intermediate durable boundary had existed.

## Crash and restart semantics

The program host checkpoints `running` before external worker dispatch. Worker result reconciliation checkpoints `review_required` or `repair_required`. Preparing a repair then creates the next attempt and checkpoints it before that new attempt can be dispatched.

Consequently:

1. a crash after `running` but before worker result leaves a durable running record for worker recovery/reconciliation;
2. a crash after a failed result but before repair preparation leaves durable `repair_required` evidence;
3. a crash after repair preparation leaves exactly the next durable attempt and its deterministic work identity;
4. a restart restores only after host-task authority, ProductProject binding, checkpoint integrity, trusted-plan semantics, and coordinator restore validation all agree.

No recovery path is allowed to infer missing durable transitions from candidate-controlled hashes or a newly recomputed snapshot.

## Idempotency

Saving the same coordinator revision with the same canonical bytes returns the existing checkpoint. The same revision with different bytes fails closed. Lower revisions fail closed. Higher revisions additionally require valid predecessor lineage.

This is distinct from worker-operation idempotency: the operation ledger controls duplicate external worker execution, while the checkpoint lineage controls which orchestration state is allowed to become durable.

## Adversarial and restart evidence

Focused tests introduced with this contract:

- `tests/test_product_factory_checkpoint_authority_provenance.py`
  - candidate checkpoint cannot bootstrap its own first authority;
  - constructor injection of trusted-plan authority is rejected;
  - live authority is not rehydrated from persisted checkpoint bytes;
  - durable restore succeeds through the host-task anchor.
- `tests/test_product_factory_checkpoint_transition_lineage.py`
  - repair without a prior durable `repair_required` checkpoint is rejected;
  - skipped attempt generations are rejected;
  - recomputed same-attempt state rollback is rejected;
  - a legitimate durable failure-to-repair sequence survives process restart at attempt 2.

Repository-wide qualification remains the responsibility of exact-head Core CI and the relevant pre-human gate. This document is not itself acceptance credit and does not claim global PF12 closure.

HUMAN_TESTED=false

NVDA_VERIFIED=false
