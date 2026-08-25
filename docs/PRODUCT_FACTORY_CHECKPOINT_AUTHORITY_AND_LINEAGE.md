# Product Factory checkpoint authority and durable lineage

Status: implementation evidence for the MANUAL-DEV05 Product Factory orchestration-integrity lane.

This document narrows the durability contract for `ProductFactoryCheckpointHost` and `ProductProjectCoordinatorBinding`. It does not redefine PF acceptance gates, ProductProject ownership, repository-graph ownership, worker-adapter ownership, or human/NVDA evidence.

## Trust boundary

A coordinator checkpoint is candidate state. Its serialized bytes, work requests, trusted-plan descriptor, checksums, work IDs, timestamps, and any hash recomputed from those fields are not independent authority.

A raw trusted-plan fingerprint is therefore never accepted as a first-anchor credential by `ProductFactoryCheckpointHost.save()`. The public save API accepts only the checkpoint and host-task identity. Knowing or recomputing the candidate plan hash is insufficient to mint first authority.

For the first checkpoint in one host process, `ProductProjectCoordinatorBinding.checkpoint()` attaches a process-ephemeral keyed proof. The proof binds the exact ProductProject identity/version, immutable trusted-plan fingerprint, and the complete exact live coordinator snapshot. The checkpoint constructor cannot accept either the fingerprint or the proof. Neither field is serialized. The checkpoint host independently verifies that proof before it writes the first durable host-task anchor.

Binding the complete live snapshot prevents a valid host-issued proof from being reused after an in-memory `object.__setattr__` mutation of revision, work state, request, result/review evidence, blocker, or trusted-plan content. Such a modified checkpoint fails proof verification before the first anchor is written.

After a successful save, the Product Factory host-task payload carries two independent durable facts inside the same already-adopted task authority:

- `trusted_plan_fingerprint`: immutable attempt-one plan authority;
- `product_factory_checkpoint_head`: the exact checkpoint ID, checksum and coordinator revision that crossed the host admission boundary.

The checkpoint head is not a second store or signer. It is a thin extension of the existing Product Factory host-task authority and commits the exact admitted durable row in the same SQLite transaction as checkpoint insertion and audit. Candidate-created `created_at`, recomputed public hashes, or a self-consistent extra checkpoint row cannot replace that committed head.

Every later save first resolves the exact host-committed head and validates the proposed transition from that admitted predecessor. Every restart resolves the exact same head instead of selecting authority by wall-clock metadata. A legacy checkpoint lineage that has durable PF checkpoint rows but no committed host head fails closed and requires explicit reconciliation.

The same live proof is reused at the next security-significant authority boundary: the first durable checkpoint of every new repair generation. This does not create a second authority system. It prevents a candidate from manufacturing attempt `N + 1` by choosing a different repair goal or repository base and recomputing its work ID/checkpoint identity. A legitimate host-mediated repair may still select a newer `base_sha`; `ProductProjectCoordinatorBinding.checkpoint()` authenticates that exact new `ready` snapshot before it becomes durable, and the transaction then advances the committed host head to that exact admitted row.

The process-ephemeral proof is a host-process capability, not a Python sandbox. Its trust assumption is that untrusted Product Factory workers execute behind the worker port and do not execute arbitrary code inside the authority-owning Nika host process. If arbitrary hostile code already executes in that trusted process, Python module privacy or an in-memory key is not a security boundary; stronger process isolation belongs to the worker/sandbox lanes.

## Durable predecessor and head requirement

Static validation of one self-consistent snapshot is insufficient for long-running orchestration. For every higher-revision save, the checkpoint host validates the candidate against the exact predecessor identified by the independently committed host head.

The predecessor contract is:

- project ID, ProductProject spec version, and ProductProject row version do not drift inside one host-task lineage;
- component identity is stable;
- within one attempt, the complete work request is immutable;
- same-attempt work state may skip intermediate in-memory states only when the resulting state is transitively forward-reachable through the legal coordinator state machine;
- accepted and blocked terminal states cannot be recomputed backwards into executable states;
- an attempt may advance by exactly one generation;
- attempt `N + 1` requires attempt `N` to have been durably saved as `repair_required`;
- the first durable checkpoint for attempt `N + 1` must be `ready`, before execution can begin;
- that first durable `N + 1` checkpoint requires a valid live host proof over the exact new snapshot;
- repair keeps project, component, repository, path scope, permission ceiling, and acceptance commands unchanged;
- repair goal lineage is the previous durable goal plus exactly one non-empty `Repair:` reason;
- a newer repair `base_sha` is permitted only as part of that host-authenticated new-generation snapshot and then becomes immutable for the rest of the attempt.

The durable-head reader additionally fails closed when:

- PF checkpoint rows exist but the host task has no canonical checkpoint-head commitment;
- the committed checkpoint ID is missing;
- committed checksum or revision disagrees with the referenced row;
- two durable rows claim the same coordinator revision with different identities;
- any durable row claims a revision beyond the independently committed head.

`created_at` remains evidence metadata only. It is not predecessor or restart authority, so system-clock rollback cannot promote an older valid checkpoint above a later host-admitted revision.

Ordinary progress does not require a database write after every pure in-memory coordinator call. For example, `ready -> running -> review_required -> accepted` may be persisted as one later accepted checkpoint within the same already-durable attempt. Security-significant repair-generation creation is stricter: the prior failed attempt must already exist durably as `repair_required`, the exact new generation must carry a valid live host proof, and it must itself cross the durable boundary as `ready` before any `running` or later state is accepted.

## Clear and re-anchor semantics

`ProductFactoryCheckpointHost.clear()` is an authority reset, not merely row deletion. In one `BEGIN IMMEDIATE` transaction it:

1. deletes only Product Factory checkpoint-stage rows for the selected host task;
2. removes the durable trusted-plan fingerprint and exact checkpoint-head commitment from the host-task payload;
3. preserves unrelated host-task payload fields and checkpoints owned by foreign stages;
4. writes the clear audit event.

If revoking the host-task anchors fails, checkpoint deletion rolls back with it. Repeated clear is idempotent. After a successful clear there is no retained PF authority from the old lineage, so the next first checkpoint must again carry a genuine process-ephemeral live proof. A legacy state created by the historical row-only clear behavior cannot silently become a new candidate-created lineage.

## Crash and restart semantics

The program host checkpoints `running` before external worker dispatch. Worker result reconciliation checkpoints `review_required` or `repair_required`. Preparing a repair creates the next attempt and checkpoints it as `ready` before that new attempt is dispatched by the program host.

Consequently:

1. a crash after `running` but before worker result leaves a durable running record for worker recovery/reconciliation;
2. a crash after a failed result but before repair preparation leaves durable `repair_required` evidence;
3. a crash after host-mediated repair preparation leaves exactly the next durable `ready` attempt, its selected base, deterministic work identity, and exact host-committed checkpoint head;
4. a candidate cannot make a recomputed new repair generation durable without the live host proof, and a raw canonical row rewrite cannot make that row restart authority while the independent host head still names the previously admitted checkpoint;
5. a restart restores only after host-task plan authority, exact checkpoint-head authority, ProductProject binding, checkpoint canonicality, trusted-plan semantics, and coordinator restore validation all agree.

No recovery path is allowed to infer a missing repair-generation boundary from candidate-controlled hashes, wall-clock ordering, or a newly recomputed snapshot. After restart from durable `repair_required`, the host may legitimately prepare a fresh newer-base repair and issue a new process-local proof for that exact `ready` state before saving it.

## Concurrency and idempotency

Checkpoint saves acquire a SQLite `BEGIN IMMEDIATE` writer reservation before reading the current host anchors and exact committed predecessor. This serializes competing writers at the exact read-validate-insert-head-update boundary. Two independent connections starting from the same predecessor cannot both commit different bytes for the same coordinator revision: one commits, and the later writer re-reads that committed head and fails closed on the conflicting same-revision state.

Checkpoint insertion, trusted-plan first binding when needed, exact checkpoint-head advancement, and audit are one transaction. A failure before commit cannot leave a durable checkpoint row without its corresponding host-head authority or advance host authority without the exact row.

Saving the same coordinator revision with the same canonical bytes returns the existing checkpoint. The same revision with different bytes fails closed. Lower revisions fail closed. Higher revisions additionally require valid predecessor lineage.

This is distinct from worker-operation idempotency: the operation ledger controls duplicate external worker execution, while the checkpoint lineage controls which orchestration state is allowed to become durable and resumable.

## Adversarial, concurrency, scale and restart evidence

Focused tests introduced or extended with this contract:

- `tests/test_product_factory_checkpoint_authority_provenance.py`
  - candidate checkpoint cannot bootstrap its own first authority;
  - the removed public `trusted_plan_fingerprint=` save argument cannot be used as a matching forged first anchor;
  - constructor injection of either live authority field is rejected;
  - `object.__setattr__` with a known fingerprint plus a forged proof cannot mint authority;
  - a valid host-issued proof cannot be replayed after exact snapshot tamper;
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
- `tests/test_product_factory_repair_generation_boundary.py`
  - a newly prepared repair that is started only in memory cannot make `running` its first durable generation state;
  - rejection leaves the previous durable `repair_required` checkpoint unchanged;
  - the canonical `ProductFactoryProgramHost.prepare_repair_and_checkpoint()` path persists attempt `N + 1` as `ready` before dispatch.
- `tests/test_product_factory_repair_generation_authority.py`
  - a self-consistent candidate-created `N + 1` `ready` checkpoint without live proof is rejected before becoming durable;
  - the rejected candidate leaves attempt `N=repair_required` unchanged;
  - the same exact host-prepared repair snapshot succeeds when issued through `ProductProjectCoordinatorBinding.checkpoint()`;
  - a legitimate newer repair base remains supported and becomes durable only with the host-authenticated generation boundary.
- `tests/test_product_factory_checkpoint_head_authority.py`
  - reverse `created_at` ordering cannot change restart authority away from the committed higher revision;
  - clear atomically revokes plan/head authority, preserves unrelated task payload and foreign checkpoint stages, requires a fresh live proof, and remains idempotent;
  - an injected SQLite failure during anchor revocation rolls back checkpoint deletion;
  - a canonical raw-row `N + 1` rewrite with recomputed checksum/checkpoint ID cannot replace the independently admitted host head after restart;
  - durable rows without a host head and a tampered host-head checksum fail closed.
- existing `tests/test_product_factory_scale_recovery.py`
  - 100 components complete across ten restart waves; this is the regression that guards legal sparse checkpointing at scale.

Repository-wide qualification remains the responsibility of exact-head Core CI and the relevant pre-human gate. An AUD02 BLOCK remains unresolved until the independent auditor replays its authority oracle against the repaired exact head. This document is not itself acceptance credit and does not claim global PF12 closure.

HUMAN_TESTED=false

NVDA_VERIFIED=false
