# ProductProject Specification Mutation Durability

Status: MANUAL-DEV01 PF0/PF12 durability contract.

## Single write authority

`ProductProjectRepository.update_spec()` is the only public ProductProject specification mutation authority. Every call delegates to one transaction primitive in `product_project_spec_durability.py`. `ProductProjectSpecDurabilityService` is a compatibility façade only; it delegates to the repository and never owns a second SQL write path.

The canonical audit event remains `product_project.spec_versioned`. Retry evidence extends that event rather than introducing a second audit vocabulary for the same lifecycle transition.

## Durable retry contract

Every specification update has a durable idempotency identity. Callers may provide an explicit `idempotency_key`; legacy callers that omit it receive a deterministic internal key derived from the effective mutation input. Therefore the older method shape does not bypass the restart/idempotency invariant.

The transaction reserves the SQLite writer before checking the idempotency ledger. One commit contains the project row-version transition, immutable specification revision, durable retry receipt and canonical audit event. Retry after an ambiguous client/process failure validates the entire persisted tuple before returning without creating another revision.

The receipt binds project identity, expected row version, previous/result specification versions, result row version, canonical input fingerprint, SHA-256 of the exact stored specification, change reason and timestamp. SHA-256 is integrity/correlation evidence, not authentication or signing.

## Mutation return linearizability

The public mutation result is bound to the revision owned by that exact operation. A new write materializes its `ProductProject` result while the authoritative writer transaction still owns the exact specification and row-version transition. An idempotent replay materializes the result from the durable receipt plus the exact immutable specification row, rather than from whatever revision happens to be current later.

A later valid writer may therefore commit immediately after the first writer without changing what the first writer returns. Likewise, replaying operation A after operation B has advanced the project returns A's exact specification/version identity and does not masquerade B's current state as A's result. The canonical mutation path performs no post-commit current-state read before returning, so an already committed mutation cannot be converted into an apparent failure by unrelated later-state read I/O.

When replay reconstructs an older result after later lifecycle changes, status is derived from canonical status-change audit evidence at the receipt's exact row version. This keeps the returned historical snapshot coherent instead of combining an old specification version with a later lifecycle status.

## Fail-closed replay and specification history

Replay is rejected on key/input drift, non-exact durable integer identity, invalid timestamps, broken version lineage, receipt state ahead of the durable project, missing or malformed specification rows, digest mismatch, lineage/reason mismatch, or missing/forged/duplicate canonical audit evidence.

`ProductProjectRepository.get()` also distinguishes a missing project from a project whose declared current specification row is missing and rejects malformed current durable specification payloads instead of converting them to not-found.

`ProductProjectRepository.spec_history()` treats durable `current_spec_version` as authority. The stored revision identities must be exactly the contiguous sequence `1..current_spec_version`; a missing current revision, an internal gap, or an extra revision ahead of the current pointer fails closed. Explicit lineage must point to the immediately preceding version and carry a non-empty revision reason. Historical rows without explicit parent metadata remain readable only as legacy sequential lineage after the complete durable sequence has been proven contiguous.

## Compatibility decision

PR #166 is the MANUAL-DEV01 convergence candidate. Current-state integrity hardening from PR #173 and lifecycle restart/audit hardening from PR #181 are absorbed into the same candidate so TECH02 receives one coherent PF0/PF12 integration surface. Portable-history strict-numeric ownership remains outside this contract.

Automated evidence never sets `HUMAN_TESTED` or `NVDA_VERIFIED`.
