# PF7 Credential Broker hardening — 2026-08-23

Status: MANUAL-DEV08 implementation/evidence record. This document does not grant integration, human-testing or NVDA credit.

## Exact lineage

- Repository: `Oleksii-debug/Nika-Core`.
- Starting live `main` for this one-shot cycle: `8e2e0eb3f0f65b75e1d23b0f36ab2bf09a8477ba`.
- Lane: `MANUAL-DEV08` / PF7 Credential & Identity Broker.
- Branch: `work/manual-dev08/pf7-project-bound-protected-store`.
- PR: #162.
- Independent blocker lineage: AUD02 #175 / exact-current successors and AUD03 #190 / exact-current successors.

## REUSE -> ADAPT -> CUSTOM (thin)

**REUSE:** retain maintained Python `keyring` and the explicit Windows `WinVaultKeyring` backend. Windows Credential Manager remains the protected persistence authority. No custom cryptographic vault, plaintext fallback or new dependency is introduced.

**ADAPT:** extend the existing `CredentialBroker` / `ProtectedSecretStorePort` boundary with protected reference/lifecycle authority binding and operation-idempotent handle reconciliation. The lifecycle repair reuses the same authority target; it does not add a second authority database or vault.

**CUSTOM (thin):** Nika only defines canonical authority fingerprints, the expected ACTIVE -> REVOKED protected transition, monotonic lease operation identity, pending-operation reconciliation and fail-closed validation. SHA-256 is used as a deterministic equality commitment for non-secret credential-reference metadata, not as encryption, a password hash, a signature or proof against compromise of the Windows user account.

## Protected authority binding

A `SecretRef` generation has an independently persisted protected-store authority fingerprint over:

- authority schema identifier;
- `secret_ref`;
- `generation`;
- `project_id`;
- provider;
- purpose;
- sorted scopes;
- sorted allowed audiences;
- lifecycle state (`ACTIVE` / `REVOKED`).

`register_secret()` first proves physical material exists, then binds the canonical ACTIVE fingerprint in protected storage before broker metadata becomes authoritative. Exact retry is idempotent. A conflicting binding fails closed.

`restore()` never bootstraps authority from candidate snapshot bytes. Every restored `SecretRef`, including revoked history, must match the already protected authority binding. Rewriting project/provider/purpose/scopes/audiences/state, even together with all candidate-owned snapshot fields, cannot transfer authority, resurrect a revoked generation, or roll a rotated credential back to a stale ACTIVE generation.

The Windows adapter stores the authority fingerprint under a separate deterministic target whose human-readable `secret_ref` is SHA-256-derived rather than exposed in the target name. The raw credential target format remains unchanged, so this batch does not silently migrate existing raw-secret identities.

## Revocation, rotation and protected retirement

`retire_authority()` performs one narrow expected-value lifecycle transition on the existing protected authority target: exact ACTIVE fingerprint -> exact REVOKED fingerprint. It is idempotent for an already-completed exact transition and fails closed if protected metadata is missing, stale or belongs to a different authority. It does not decrypt or transform raw credential material.

`revoke()` authenticates the current ACTIVE authority, invalidates in-process handles, retires the protected authority to REVOKED, and only then publishes revoked broker state/audit. Retrying an already-revoked broker reference still authenticates the protected REVOKED authority.

`rotate()` requires the next raw generation to be pre-provisioned, retires the prior generation's protected ACTIVE authority before binding the next generation ACTIVE authority, then publishes the new broker generation. If a process observes that the old retirement effect already completed after an interrupted same-process attempt, the exact retired fingerprint is accepted as reconciliation; any other protected value fails closed. A stale pre-rotation ACTIVE snapshot therefore cannot regain authority even while old raw material still physically exists.

Raw material may be deleted after revocation/rotation while the non-secret REVOKED fingerprint remains available to authenticate durable history. Final cleanup is explicit: `delete_authority()` is rejected while raw material still exists and may remove authority metadata only after material has been removed.

This batch intentionally does not claim atomic multi-process credential lifecycle updates. Windows Credential Manager/keyring has no native compare-and-set transaction spanning independent Nika processes. The existing single credential-authority host assumption remains binding; interruption between lifecycle steps fails closed rather than silently restoring stale authority.

## Ambiguous handle effects and retries

`CredentialBroker` allocates the next monotonic `credential-lease-*` identity before calling the protected store and keeps an in-process pending request until the effect is known. The lease identity becomes the protected-store `operation_id`.

The protected store makes `issue_handle(operation_id=...)` idempotent for the exact binding and exposes `reconcile_handle()` for an uncertain outcome. If the store reports an exception after creating the handle, the broker reconciles the exact operation instead of blindly creating another handle. If no exact effect can be reconciled, the request fails closed and a different lease request is blocked until the original pending request is retried or process authority is reset by restart.

The Windows adapter records an in-process `operation_id -> handle` binding. Exact retries return the same handle; reuse of the same operation identity with changed project/audience/scopes/generation/expiry fails closed. Revocation, expiry or missing material removes both handle and operation mapping. Process restart intentionally drops handle/operation authority while OS material and protected authority fingerprints survive.

## Concurrency

Broker mutable credential state is serialized by an in-process `RLock`. This prevents concurrent lease calls from racing the monotonic lease counter/pending operation and preserves unique deterministic lease identities. Windows store material, authority, handle and operation mutations remain protected by its existing reentrant lock.

This still does not claim atomic cross-process provisioning or lifecycle mutation. A later design may add a supported cross-process authority lock only if Product Factory ownership requires multiple independent credential-authority hosts.

## Other hardened invariants retained

1. No public credential enumeration; callers must already hold the opaque `SecretRef` identity.
2. Cross-project exact lookup and lease substitution fail closed.
3. ACTIVE restore additionally requires physical protected material to still exist.
4. REVOKED history may restore without old raw material, but only with the matching protected REVOKED authority.
5. Lease authority and raw handles remain absent from broker snapshots.
6. Generation/TTL counters require exact positive integers and reject Python boolean aliases.
7. The protected store is excluded from `CredentialBroker.__repr__`.
8. Audience/scopes remain attenuated to the registered policy.
9. Audit evidence carries reference metadata only and never serializes raw secret material.
10. No candidate snapshot may rewrite lifecycle state to widen effective credential authority.

## Adversarial qualification

Owner regressions cover:

- AUD02-style fully self-consistent project rewrite while retaining the same physical generation;
- provider, purpose, scope and audience policy forgery;
- candidate REVOKED -> ACTIVE resurrection while raw material remains;
- stale pre-rotation ACTIVE generation rollback after a newer generation becomes current;
- protected authority binding/retirement conflict and restart persistence;
- revoked-history authority continuity after raw-material deletion;
- AUD03-style handle effect followed by acknowledgement/transport exception;
- exact operation reconciliation without duplicate handle authority;
- unreconciled failure blocking a different request until exact retry;
- conflicting operation reuse;
- concurrent monotonic lease issuance;
- prior expiry/revoke/rotation/project-isolation/audit-counter/snapshot/repr non-leakage suites.

The physical Windows proof additionally verifies ACTIVE authority persistence across adapter reconstruction, durable ACTIVE -> REVOKED retirement across another adapter reconstruction, exact retirement retry, operation idempotency/reconciliation, 2560-byte credential material, process-handle invalidation, packaged PyInstaller behavior and cleanup.

## Security / secret truth

- No real API key, OAuth token, cookie, browser profile, password or account credential is committed or printed.
- Test values are synthetic fixtures.
- No OS credential enumeration API is introduced.
- No raw-secret getter or worker redemption API is introduced.
- No provider/cloud/account action is executed by this batch.
- M10 authorization/approval production source is unchanged.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- QA_ONLY proof/audit vehicles must never be merged.

## Acceptance evidence rule

Only terminal Actions attached to the final exact PR head count. Core CI and complete M12 must succeed on that same SHA. Because this repair changes `product_factory_windows_credentials.py` and the physical proof contract, a real focused Windows Credential Manager proof is required on the same candidate in both packaged ctypes/cffi paths; historical proof does not transfer. Independent AUD02 and AUD03 exact-head replay must close their findings before guarded production integration.
