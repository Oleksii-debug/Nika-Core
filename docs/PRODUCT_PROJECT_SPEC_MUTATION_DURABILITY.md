# ProductProject Specification Mutation Durability

Status: MANUAL-DEV01 PF0/PF12 durability contract.

## Single write authority

`ProductProjectRepository.update_spec()` is the only public ProductProject specification mutation authority. Every call delegates to one transaction primitive in `product_project_spec_durability.py`. `ProductProjectSpecDurabilityService` is a compatibility façade only; it delegates to the repository and never owns a second SQL write path.

The canonical audit event remains `product_project.spec_versioned`. Retry evidence extends that event rather than introducing a second audit vocabulary for the same lifecycle transition.

## Durable retry contract

Every specification update has a durable idempotency identity. Callers may provide an explicit `idempotency_key`; legacy callers that omit it receive a deterministic internal key derived from the effective mutation input. Therefore the older method shape does not bypass the restart/idempotency invariant.

The transaction reserves the SQLite writer before checking the idempotency ledger. One commit contains the project row-version transition, immutable specification revision, durable retry receipt and canonical audit event. Retry after an ambiguous client/process failure validates the entire persisted tuple before returning without creating another revision.

The receipt binds project identity, expected row version, previous/result specification versions, result row version, canonical input fingerprint, SHA-256 of the exact stored specification, change reason and timestamp. SHA-256 is integrity/correlation evidence, not authentication or signing.

## Fail-closed replay

Replay is rejected on key/input drift, non-exact durable integer identity, invalid timestamps, broken version lineage, receipt state ahead of the durable project, missing or malformed specification rows, digest mismatch, lineage/reason mismatch, or missing/forged/duplicate canonical audit evidence.

`ProductProjectRepository.get()` also distinguishes a missing project from a project whose declared current specification row is missing and rejects malformed current durable specification payloads instead of converting them to not-found.

## Compatibility decision

PR #166 is the MANUAL-DEV01 convergence candidate. Current-state integrity hardening from PR #173 and lifecycle restart/audit hardening from PR #181 are absorbed into the same candidate so TECH02 receives one coherent PF0/PF12 integration surface. Portable-history strict-numeric ownership remains outside this contract.

Automated evidence never sets `HUMAN_TESTED` or `NVDA_VERIFIED`.
