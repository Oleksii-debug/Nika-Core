# PF7 Credential Broker hardening — 2026-08-23

Status: MANUAL-DEV08 implementation/evidence record. This document does not grant integration, human-testing or NVDA credit.

## Exact lineage

- Repository: `Oleksii-debug/Nika-Core`.
- Starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
- Current compatible live-main baseline during this repair: `e40691a6e2ff9c31fd413f63d004612e048d95ed`.
- Lane: `MANUAL-DEV08` / PF7 Credential & Identity Broker.
- Branch: `work/manual-dev08/pf7-project-bound-protected-store`.
- PR: #162.
- Independent blockers that triggered this repair: AUD02 #175 and AUD03 #190.

## REUSE -> ADAPT -> CUSTOM (thin)

**REUSE:** retain maintained Python `keyring` and the explicit Windows `WinVaultKeyring` backend. Windows Credential Manager remains the protected persistence authority. No custom cryptographic vault, plaintext fallback or new dependency is introduced.

**ADAPT:** extend the existing `CredentialBroker` / `ProtectedSecretStorePort` boundary with two security semantics that the candidate-owned broker snapshot cannot provide by itself: protected immutable reference-authority binding and operation-idempotent handle reconciliation.

**CUSTOM (thin):** Nika only defines canonical policy fingerprints, monotonic lease operation identity, pending-operation reconciliation and fail-closed validation. SHA-256 is used as a deterministic equality commitment for public credential-reference metadata, not as encryption, a password hash, a signature or proof against compromise of the Windows user account.

## Protected authority binding

A `SecretRef` generation now has an independently persisted protected-store authority fingerprint over:

- `secret_ref`;
- `generation`;
- `project_id`;
- provider;
- purpose;
- sorted scopes;
- sorted allowed audiences;
- authority schema identifier.

The mutable runtime state (`ACTIVE` / `REVOKED`) is deliberately excluded because revocation changes lifecycle state without changing who owns the credential or what policy originally bound the generation.

`register_secret()` first proves physical material exists, then binds the canonical fingerprint in protected storage before broker metadata becomes authoritative. Exact retry is idempotent. A conflicting binding fails closed. `rotate()` establishes the next generation's protected authority before invalidating the prior generation.

`restore()` never bootstraps authority from candidate snapshot bytes. Every restored `SecretRef`, including revoked history, must match the already protected authority binding. Therefore an attacker who rewrites project/provider/purpose/scopes/audiences and also recomputes all candidate-owned snapshot fields cannot transfer credential authority to another ProductProject.

The Windows adapter stores the authority fingerprint under a separate deterministic target whose human-readable `secret_ref` is still SHA-256-derived rather than exposed in the target name. The raw credential target format remains unchanged, so this batch does not silently migrate existing raw-secret identities.

## Revocation and physical retirement

Raw material may be deleted after revocation while the non-secret protected authority fingerprint remains available to authenticate durable history. Final cleanup is explicit: `delete_authority()` is rejected while raw material still exists, and may retire the authority metadata only after material has been removed. The physical QA proof cleans up both generations and their authority targets.

## Ambiguous handle effects and retries

`CredentialBroker` allocates the next monotonic `credential-lease-*` identity before calling the protected store and keeps an in-process pending request until the effect is known. The lease identity becomes the protected-store `operation_id`.

The protected store must make `issue_handle(operation_id=...)` idempotent for the exact binding and expose `reconcile_handle()` for an uncertain outcome. If a provider/store reports an exception after creating the handle, the broker reconciles the exact operation instead of blindly creating another handle. If no exact effect can be reconciled, the request fails closed and a different lease request is blocked until the original pending request is retried or process authority is reset by restart.

The official Windows adapter records an in-process `operation_id -> handle` binding. Exact retries return the same handle; reuse of the same operation identity with changed project/audience/scopes/generation/expiry fails closed. Revocation, expiry or missing material removes both handle and operation mapping. Process restart intentionally drops handle/operation authority while OS material and the immutable authority fingerprint survive.

## Concurrency

Broker mutable credential state is serialized by an in-process `RLock`. This prevents concurrent lease calls from racing the monotonic lease counter/pending operation and preserves unique deterministic lease identities. Windows store material, authority, handle and operation mutations remain protected by its existing reentrant lock.

This still does not claim atomic cross-process provisioning. Windows Credential Manager/keyring does not provide a compare-and-set primitive for two independent Nika processes. The existing single credential-authority host assumption remains binding unless a later cross-process authority lock is added and proven.

## Other hardened invariants retained

1. No public credential enumeration; callers must already hold the opaque `SecretRef` identity.
2. Cross-project exact lookup and lease substitution fail closed.
3. Active restore additionally requires physical protected material to still exist.
4. Revoked history may restore without old raw material, but only with the matching protected authority binding.
5. Lease authority and raw handles remain absent from broker snapshots.
6. Generation/TTL counters require exact positive integers and reject Python boolean aliases.
7. The protected store is excluded from `CredentialBroker.__repr__`.
8. Audience/scopes remain attenuated to the registered policy.
9. Audit evidence carries reference metadata only and never serializes raw secret material.

## Adversarial qualification

Owner regressions now cover the independent findings directly:

- AUD02-style fully self-consistent project rewrite while retaining the same physical generation;
- provider, purpose, scope and audience policy forgery;
- authority binding conflict and restart persistence;
- revoked-history authority continuity after raw-material deletion;
- AUD03-style handle effect followed by acknowledgement/transport exception;
- exact operation reconciliation without duplicate handle authority;
- unreconciled failure blocking a different request until exact retry;
- conflicting operation reuse;
- concurrent monotonic lease issuance;
- prior expiry/revoke/rotation/project-isolation/audit-counter/snapshot/repr non-leakage suites.

The physical Windows proof additionally verifies authority persistence across adapter reconstruction, operation idempotency/reconciliation, 2560-byte credential material, process-handle invalidation, packaged PyInstaller behavior and cleanup.

## Security / secret truth

- No real API key, OAuth token, cookie, browser profile, password or account credential is committed or printed.
- Test values are synthetic fixtures.
- No OS credential enumeration API is introduced.
- No raw-secret getter or worker redemption API is introduced.
- No provider/cloud/account action is executed by this batch.
- M10 authorization/approval production source is unchanged.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- Integration remains owned by TECH02; MANUAL-DEV08 does not self-merge.

## Acceptance evidence rule

Only terminal Actions attached to the final exact PR head count. Core CI and complete M12 must succeed on that same SHA. Because this repair changes `product_factory_windows_credentials.py`, a real focused Windows Credential Manager proof is required on the same candidate as well; a historical skip or an older physical proof does not transfer. Independent AUD02/AUD03 replay must close their findings before TECH02 integration credit.
