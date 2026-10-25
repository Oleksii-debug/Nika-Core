# PF9 Business Factory — current-main convergence

## Scope

This slice implements the PF9 business-intake core on top of the canonical ProductProject
repository. It covers:

`BusinessObjective -> evidence-backed MarketOpportunity -> policy-bounded Lead ->
qualified Proposal -> trusted proposal approval -> trusted WorkOrder ->
durable ProductProject handoff`.

It does **not** add an external communication executor, contract signer, account manager,
payment executor, deployment executor, or PF10 release/compliance gate.

## Reuse / adapt / custom decision

- **REUSE** `ResearchEvidencePackage` / `EvidenceRef` for research provenance.
- **REUSE** `ProductProjectSpec` and `ProductProjectRepository` for the actual product effect,
  persistence, and idempotency ledger.
- **ADAPT** the PF9 authority lineage from PR #195 and exact initial-spec binding from PR #349.
- **CUSTOM (thin)** binds the WorkOrder to the exact target ProductProject identity
  (`project_id`, name) in addition to the normalized initial ProductProject spec fingerprint,
  and adds a PF9-only snapshot repository with optimistic concurrency.

No second Product Factory, generic workflow engine, signer, policy engine, or side-effect
framework is introduced.

## Authority boundary

Proposal approval and WorkOrder creation use `BusinessAuthorizationAuthorityPort`.

The production default is fail-closed: with no trusted host authority, positive proposal or
WorkOrder authorization is impossible. Evidence is supplied only as an opaque reference.
The authority implementation is responsible for current/revoked state and one-time versus
standing-policy reuse.

A WorkOrder authorization fingerprint binds:

- business objective;
- approved proposal identity and proposal approval fingerprint;
- exact WorkOrder scope;
- target ProductProject ID and name;
- SHA-256 of the normalized initial `ProductProjectSpec`.

The business worker cannot widen contract authority beyond `APPROVAL_REQUIRED` and cannot
widen financial authority beyond `RECORD_ONLY`.

## ProductProject handoff integrity

The caller-provided `ProductProjectSpec` must contain
`compliance["business_work_order_ref"]` for the exact WorkOrder and cannot pre-populate
trusted PF9 lineage fields.

The ProductProject effect uses a deterministic operation key derived only from
`(objective_id, work_order_id)`. Therefore:

- an exact retry reconciles the same ProductProject;
- a crash after ProductProject commit but before PF9 acknowledgement does not create a second
  product;
- changing the caller request key does not create a second effect;
- a conflicting ProductProject ID/name/spec under the same WorkOrder fails closed.

The persisted ProductProject receives trusted PF9 lineage for the WorkOrder authorization,
authorized initial-spec fingerprint, objective, and deterministic handoff effect key.

## Durability and concurrency

`BusinessFactoryRepository` uses a separately versioned PF9 SQLite namespace and the existing
`SQLiteStore.connection()` boundary.

Snapshots carry a monotonic `row_version` equal to their contiguous audit-event count.
Writes use compare-and-swap semantics:

- first writer uses `expected_row_version=0`;
- later writes must match the durable row version;
- a stale/concurrent writer receives `StaleBusinessStateError`;
- load verifies objective identity and row-version metadata against the serialized snapshot.

The snapshot format is `nika.business_factory.pf9.v1`. Unsupported or structurally different
payloads fail closed.

## Safety and product truth

This slice only creates local durable domain state and a ProductProject record. It does not
perform a real external send, sign a contract, spend money, create an external account,
deploy software, or issue a release.

`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.

PF9 as a whole still requires downstream Product Factory delivery, communication/delivery
state, PF10/release-compliance integration where applicable, packaged Product Journey
evidence, and the project acceptance gates before release credit.
