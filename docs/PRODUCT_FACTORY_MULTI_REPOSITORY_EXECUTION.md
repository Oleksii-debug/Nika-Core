# Product Factory multi-repository execution slice

Status: ONE-SHOT-18 implementation contract.
Starting `main`: `3fbfabfc93d59183f174ff44098db886cff93bd8`.

## Scope

This slice closes one durable ProductProject execution path across multiple repositories without
introducing another monorepo framework, scheduler, coding-worker runtime, or lease database.

The implementation composes existing canonical contracts:

- `ProductProject` remains the durable product/specification owner.
- `ProductRepositoryGraph` remains the repository/component/path/dependency authority.
- `DynamicTeamComposer` remains the team-shaping contract; this slice does not create a second
  team persistence system.
- `CodingWorkerComponentAdapter` remains the public Product Factory bridge to
  `CodingWorkerPort`.
- `ProductFactoryProgramHost` remains the crash-consistent bounded parallel worker host.
- the canonical SQLite `tasks`, `checkpoints`, idempotency and audit tables are reused; there is
  no schema migration.

## REUSE -> ADAPT -> CUSTOM(thin)

**REUSE:** ProductRepositoryGraph validation/leases/dependency order, ProductProject binding,
CodingWorker bridge, ProgramHost dispatch/recovery, generic SQLite checkpoint/audit storage.

**ADAPT:** persist the exact ProductRepositoryGraph as a host-task authority and reconstruct it
before restoring coordinator work; derive durable running ownership leases from coordinator
RUNNING records; add a high-level repair call that removes caller control of `base_sha`.

**CUSTOM(thin):** graph authority serialization/checksum/host-task fingerprint,
versioned dependency-edge evidence, and repair-lineage intent records. These are Nika-specific
durable identity semantics that the worker/runtime cannot own.

No new third-party dependency is added.

## Durable graph authority

`MultiRepositoryProductFactoryHost.initialize()` binds exactly one graph authority to one
Product Factory host task and current ProductProject `(project_id, spec_version, row_version)`.

The graph digest covers complete repository/component data exposed by the canonical graph,
including repository locators/path case policy and component ownership, dependency, build/test
commands and release identity. Dependency edges are persisted with an explicit `graph_version`
and recomputed on restore.

The host task stores an independent graph-authority fingerprint. A graph checkpoint with a
recomputed payload checksum cannot silently replace the bound graph, graph version or
ProductProject version.

Graph changes are deliberately not an in-place mutation API in this slice. A changed
ProductProject/specification requires explicit reconciliation/new authority rather than silently
rewriting the running graph.

## Ownership and parallelism

A work attempt receives an `OwnershipLease` derived from its exact `work_id`, component and
allowed paths. RUNNING leases are reconstructed from durable coordinator state after restart;
they are not duplicated in another lease store.

Before dispatch, every READY attempt is checked with `ProductRepositoryGraph.assess_lease()`
against:

1. leases reconstructed from current RUNNING records;
2. caller-supplied already-active external graph leases, when present;
3. earlier READY attempts selected for the same dispatch wave.

Any overlap fails before worker dispatch with deterministic conflict identity. An explicit
integration decision can be handled by the canonical graph/integration layer, but this host does
not run overlapping writers in parallel.

Independent repositories/components continue through `ProductFactoryProgramHost`, which
already performs bounded parallel dispatch and isolates one external worker exception from
siblings. Dependency integration order remains the coordinator's canonical rule: downstream
work becomes READY only after every declared dependency is independently ACCEPTED.

Changed-file scope is still enforced by `CodingWorkerComponentAdapter` /
`AllowedPathPolicy`; this module does not duplicate worker path validation.

## Exact repair lineage

`MultiRepositoryProductFactoryHost.prepare_repair_and_checkpoint()` intentionally has no
`base_sha` argument.

For a `REPAIR_REQUIRED` component it:

1. reads the exact durable prior `WorkerResultEnvelope.result_sha`;
2. previews the next canonical coordinator request and restores the in-memory snapshot;
3. persists an immutable repair-lineage intent binding previous attempt/work/result to exact next
   attempt/work/base and reason;
4. calls the existing ProgramHost to persist the next READY generation;
5. verifies the created request matches the durable intent.

The intent is written before generation advancement. A crash before the ProgramHost checkpoint
leaves one valid pending intent; a retry is idempotent. A generation advanced through a lower
level without matching lineage fails closed when this multi-repository host restores it.

Lineage validation is anchored to the coordinator's immutable `trusted_plan` initial work IDs
and requires a contiguous attempt-by-attempt chain.

## Restart contract

`restore()` takes the current ProductProject and `host_task_id`; it does **not** take a caller
repository graph. It reloads and validates the bound graph authority, reconstructs
`ProductProjectCoordinatorBinding`, restores the canonical ProgramHost checkpoint and validates
repair lineage.

The deterministic acceptance fixture performs 30 consecutive fresh SQLite/host reconstructions
after completing the four-repository flow and requires exact graph digest, versioned dependency
edges, complete coordinator snapshot and repair lineage to remain unchanged.

## Deterministic real-Git fixture

`tests/test_product_factory_multi_repository_execution.py` creates four actual temporary Git
repositories with deterministic initial commits:

- API;
- assets;
- SDK depending on API;
- desktop depending on SDK and assets.

The first wave runs API and assets concurrently. Assets returns a bounded retryable worker
failure while API proceeds to independent review. After API acceptance, the SDK can progress
while the assets repair advances only from the exact failed result SHA. Desktop remains blocked
until both dependencies are accepted.

The test uses the real `CodingWorkerComponentAdapter` and `ProductFactoryProgramHost`; only
external worker effects/evidence are provided by a deterministic local test worker. Additional
tests prove:

- identical physical repository locator cannot masquerade as two logical repository IDs;
- deterministic lease conflict output is independent of active-lease input order;
- changed-file evidence outside component ownership is rejected while a sibling repository
  continues;
- recomputed graph-checkpoint tampering is rejected by host-task graph authority;
- low-level repair advancement without explicit lineage fails closed on restart (or is rejected
  earlier by any stronger checkpoint authority integrated later).

## Acceptance truth

This is a backend Product Factory execution slice. It does not claim PF11, packaging,
deployment, HUMAN_TESTED, NVDA_VERIFIED or production release readiness. It adds no UI surface
and therefore creates no new automated NVDA claim.
