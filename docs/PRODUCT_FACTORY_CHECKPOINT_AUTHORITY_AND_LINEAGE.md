# Product Factory checkpoint authority and durable lineage

Status: implementation evidence for the MANUAL-DEV05 Product Factory orchestration-integrity lane.

This document narrows the durability contract for `ProductFactoryCheckpointHost` and `ProductProjectCoordinatorBinding`. It does not redefine PF acceptance gates, ProductProject ownership, repository-graph ownership, worker-adapter ownership, or human/NVDA evidence.

## Trust boundary

A coordinator checkpoint is candidate state. Its serialized bytes, work requests, trusted-plan descriptor, checksums, work IDs, and any hash recomputed from those fields are not independent authority.

A raw trusted-plan fingerprint is therefore never accepted as a first-anchor credential by `ProductFactoryCheckpointHost.save()`. The public save API accepts only the checkpoint and host-task identity. Knowing or recomputing the candidate plan hash is insufficient to mint first authority.

For the first checkpoint in one host process, `ProductProjectCoordinatorBinding.checkpoint()` attaches a process-ephemeral keyed proof that binds the exact ProductProject identity/version and trusted-plan fingerprint. The checkpoint constructor cannot accept either the fingerprint or the proof. Neither field is serialized. The checkpoint host independently verifies that proof before it writes the first durable host-task anchor.

After the first successful save, the canonical restart authority is the fingerprint anchored in the Product Factory host-task payload. Every later save and restore validates the candidate snapshot against that durable host-task anchor. A legacy checkpoint that exists without that anchor fails closed and requires explicit reconciliation.

The process-ephemeral proof is a host-process capability, not a Python sandbox. Its trust assumption is that untrusted Product Factory workers execute behind the worker port and do not execute arbitrary code inside the authority-owning Nika host process. If arbitrary hostile code already executes in that trusted process, Python module privacy or an in-memory key is not a security boundary; stronger process isolation belongs to the worker/sandbox lanes.

## Durable predecessor requirement

Static validation of one self-consistent snapshot is insufficient for long-running orchestration. For every higher-revision save, the checkpoint host also validates the candidate against the latest durable predecessor for the same host task.

The predecessor contract is:

- project ID, ProductProject spec version, and ProductProject row version do not drift inside one host-task lineage;
- component identity is stable;
- within one attempt, the complete work request is immutable;
- same-attempt work state may skip intermediate in-memory states only when the resulting state is transitively forward-reachable through the legal coordinator state machine;
- accepted and blocked terminal states cannot be recomputed backwards into executable states;
- an attempt may advance by exactly one generation;
- attempt `N + 1` requires attempt `N` to have been durably saved as `repair_required`;
- repair keeps project, component, repository, path scope, permission ceiling, and acceptance commands unchanged;
- repair goal lineage is the previous durable goal plus exactly one non-empty `Repair:` reason.

Ordinary progress does not require a database write after every pure in-memory coordinator call. For example, `ready -> running -> review_required -> accepted` may be persisted as one later accepted checkpoint. Security-significant repair-generation creation is stricter: the prior failed attempt must already exist durably as `repair_required` before the next attempt can become durable.

## Crash and restart semantics

The program host checkpoints `running` before external worker dispatch. Worker result reconciliation checkpoints `review_required` or `repair_required`. Preparing a repair creates the next attempt and checkpoints it before that new attempt is dispatched by the program host.

Consequently:

1. a crash after `running` but before worker result leaves a durable running record for worker recovery/reconciliation;
2. a crash after a failed result but before repair preparation leaves durable `repair_required` evidence;
3. a crash after repair preparation leaves exactly the next durable attempt and its deterministic work identity;
4. a restart restores only after host-task authority, ProductProject binding, checkpoint integrity, trusted-plan semantics, and coordinator restore validation all agree.

No recovery path is allowed to infer a missing repair-generation boundary from candidate-controlled hashes or a newly recomputed snapshot.

## Concurrency and idempotency

Checkpoint saves acquire a SQLite `BEGIN IMMEDIATE` writer reservation before reading the current host anchor and latest predecessor. This serializes competing writers at the exact read-validate-insert boundary. Two independent connections starting from the same predecessor cannot both commit different bytes for the same coordinator revision: one commits, and the later writer re-reads that committed predecessor and fails closed on the conflicting same-revision state.

Saving the same coordinator revision with the same canonical bytes returns the existing checkpoint. The same revision with different bytes fails closed. Lower revisions fail closed. Higher revisions additionally require valid predecessor lineage.

This is distinct from worker-operation idempotency: the operation ledger controls duplicate external worker execution, while the checkpoint lineage controls which orchestration state is allowed to become durable.

## Adversarial, concurrency, scale and restart evidence

Focused tests introduced or extended with this contract:

- `tests/test_product_factory_checkpoint_authority_provenance.py`
  - candidate checkpoint cannot bootstrap its own first authority;
  - the removed public `trusted_plan_fingerprint=` save argument cannot be used as a matching forged first anchor;
  - constructor injection of either live authority field is rejected;
  - `object.__setattr__` with a known fingerprint plus a forged proof cannot mint authority;
  - live proof/fingerprint are not rehydrated from persisted checkpoint bytes;
  - durable restore succeeds through the host-task anchor.
- `tests/test_product_factory_checkpoint_transition_lineage.py`
  - repair without a prior durable `repair_required` checkpoint is rejected;
  - skipped attempt generations are rejected;
  - recomputed same-attempt state rollback is rejected;
  - sparse legal `ready -> accepted` checkpoint progress remains supported;
  - conflicting writers over two independent SQLite connections serialize to one durable next revision;
  - a legitimate failure-to-repair sequence survives process restart;
  - 25 repair generations survive repeated store reconstruction/restart while preserving the original trusted-plan authority.
- existing `tests/test_product_factory_scale_recovery.py`
  - 100 components complete across ten restart waves; this is the regression that guards legal sparse checkpointing at scale.

Repository-wide qualification remains the responsibility of exact-head Core CI and the relevant pre-human gate. An AUD02 BLOCK remains unresolved until the independent auditor replays its authority oracle against the repaired exact head. This document is not itself acceptance credit and does not claim global PF12 closure.

HUMAN_TESTED=false

NVDA_VERIFIED=false
