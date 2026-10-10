# PF7 Credential Broker hardening — 2026-08-23

Status: MANUAL-DEV08 implementation/evidence record. This document does not grant integration, human-testing or NVDA credit.

## Exact lineage

- Repository: `Oleksii-debug/Nika-Core`.
- Original PF7 cycle start: `8e2e0eb3f0f65b75e1d23b0f36ab2bf09a8477ba`.
- Current NIKA50 repair cycle started from live `main` `0b9b86f728343bea8f630847c1d1ec2168d8ea04`; main later advanced through an accidental direct-main no-op/revert and remains an external governance dependency.
- Lane: `MANUAL-DEV08` / PF7 Credential & Identity Broker.
- Branch: `work/manual-dev08/pf7-project-bound-protected-store`.
- Production PR: #162.
- Current independent security oracle: QA_ONLY / DO_NOT_MERGE #432.
- Physical Windows evidence vehicle: QA_ONLY / DO_NOT_MERGE #221.

## REUSE -> ADAPT -> CUSTOM (thin)

**REUSE:** maintained Python `keyring` with the explicit Windows `WinVaultKeyring` backend. Windows Credential Manager remains the protected persistence authority. No custom cryptographic vault, plaintext fallback, second credential database or new dependency is introduced.

**ADAPT:** the existing `CredentialBroker` / `ProtectedSecretStorePort` boundary now separates trusted protected-store enrollment from caller-facing broker registration, binds opaque handles to the exact protected lifecycle authority observed at issuance, and uses the Windows kernel object namespace for one live per-user credential-authority process across logon sessions.

**CUSTOM (thin):** Nika defines only canonical authority fingerprints, ACTIVE -> REVOKED protected transitions, monotonic lease operation identity/reconciliation, exact provider binding, handle-to-authority validation and a narrow Windows ownership primitive. SHA-256 is a deterministic equality commitment for non-secret metadata/identity bytes, not encryption, password hashing or a signature.

## Trusted protected authority enrollment

A `SecretRef` generation has a protected-store authority fingerprint over:

- authority schema identifier;
- `secret_ref` and generation;
- ProductProject identity;
- provider and purpose;
- exact sorted scopes;
- exact sorted allowed audiences;
- lifecycle state (`ACTIVE` / `REVOKED`).

Raw credential material and credential authority are deliberately separate prerequisites.

`CredentialBroker.register_secret()` **does not create the first authority binding**. Registration requires:

1. the referenced raw generation already exists in protected storage; and
2. the exact canonical ACTIVE fingerprint has already been enrolled through the trusted protected-store capability.

If material exists but authority is absent or differs, registration fails closed. Therefore a caller-created `SecretRef` cannot choose project/provider/purpose/scopes/audiences and make those same strings authoritative merely by calling `register_secret()`.

`credential_authority_fingerprint()` is a pure deterministic metadata helper for trusted provisioning. Knowing or computing the fingerprint grants no authority; the protected-store enrollment capability is the authority boundary.

Rotation remains host-mediated: it authenticates the already trusted current generation, retires that authority, requires the next raw generation to exist, then binds the next generation with the inherited trusted policy. This does not reopen first-registration bootstrap.

`restore()` never bootstraps authority from candidate snapshot bytes. Every restored reference, including revoked history, must match protected authority. Self-consistently rewriting snapshot project/provider/purpose/scopes/audiences/state therefore cannot transfer or resurrect credential authority.

## Revocation, rotation and protected retirement

`retire_authority()` performs one narrow expected-value transition on the protected authority target: exact ACTIVE fingerprint -> exact REVOKED fingerprint. Exact retry is idempotent; missing/stale/conflicting protected metadata fails closed.

`revoke()` authenticates current ACTIVE authority, invalidates local broker/store handles, retires protected authority and only then publishes revoked broker state/audit. `rotate()` similarly retires the predecessor before publishing the next ACTIVE generation. A stale pre-revoke or pre-rotation ACTIVE snapshot cannot regain authority even while old raw material still exists.

Raw material may be deleted after retirement while the non-secret REVOKED fingerprint remains to authenticate durable history. `delete_authority()` is rejected while raw material still exists.

## Opaque handle lifecycle authority

A Windows opaque handle is process-ephemeral and now stores the exact protected authority fingerprint observed at handle issuance in addition to operation ID, secret/generation, project, audience, scopes and expiry.

`issue_handle()` requires both raw material and an existing canonical protected authority. Exact operation retry remains idempotent.

`reconcile_handle()` recomputes the current protected authority before accepting the operation binding. If lifecycle authority changed, the old operation/handle binding cannot be reconciled as current.

`validate_handle()` re-reads current protected authority on every redemption. If another `WindowsCredentialStore` instance sharing the same WinVault backend retired or rotated that generation, a peer adapter's stale in-memory handle is removed and rejected even though raw material may still physically exist. Broker-side revoke is therefore not the sole invalidation mechanism.

Process restart intentionally loses opaque handles and operation indexes while raw material and protected authority survive.

## Ambiguous handle effects and retries

`CredentialBroker` allocates the next `credential-lease-*` operation identity before protected-store handle creation and retains one pending request while outcome is uncertain.

The store makes `issue_handle(operation_id=...)` idempotent for the exact binding and exposes `reconcile_handle()` for an uncertain acknowledgement. An effect followed by an exception is reconciled to the same handle rather than duplicated. If no exact effect can be proven, the request fails closed and a different request cannot consume the pending identity.

## Cross-session single credential-authority process

PF7 no longer relies on an in-process `RLock` alone and no longer uses a session-local `Local\\...` owner object.

On real Windows, adapter initialization obtains the primary process token's user SID, hashes the SID bytes, and creates a named event:

`Global\\NikaCore.ProductFactory.CredentialAuthority.<user-sid-hash>.<service-prefix-hash>`

Properties:

- `Global\\` makes the ownership object visible across Terminal Services / Fast User Switching / RDP logon sessions.
- The user SID hash gives distinct normal users distinct ownership identities without exposing the SID in evidence/logging.
- `CreateEventW` atomically creates or opens the named event. `ERROR_ALREADY_EXISTS` is treated as another active credential-authority host and fails closed.
- The event uses the creator token's default DACL; an inaccessible conflicting object also fails closed rather than creating a second owner.
- The process retains the kernel handle for its lifetime. Normal exit or crash causes Windows to close the process handle; once no handle remains, a later process may become owner.
- No PID lockfile, writable environment-derived path, admin privilege, custom daemon, additional database or cryptographic ownership protocol is introduced.
- Multiple `WindowsCredentialStore` instances in the same process reuse the one module-held owner handle.

The in-process `RLock` remains responsible for thread serialization inside the single owner process; the named event prevents a second process for the same Windows user/service from becoming a concurrent credential-authority host.

Non-Windows unit tests do not pretend to own a Win32 kernel object. The physical Windows proof remains mandatory for this boundary.

## Provider binding retained

Deployment execution resolves the protected `SecretRef` before lease issuance and requires its exact provider to match the environment provider identity in the supported forms `provider`, `provider:<provider>` or `provider://<provider>`. Provider mismatch becomes `BLOCKED_CREDENTIAL` before a lease/use effect.

## Other hardened invariants retained

1. No public credential enumeration; callers must already know the opaque reference identity.
2. Cross-project lookup and lease substitution fail closed.
3. ACTIVE restore additionally requires physical protected material.
4. REVOKED history may restore without old raw material only with matching protected REVOKED authority.
5. Broker snapshots contain neither raw handles nor lease authority.
6. Generation/TTL counters require exact positive integers and reject Boolean aliases.
7. Protected stores are excluded from broker `repr`.
8. Audience/scopes are attenuated to registered policy.
9. Audit/evidence surfaces contain reference metadata only, never raw secret material.
10. Candidate snapshot bytes cannot mint or widen protected authority.

## Adversarial qualification

Owner regressions cover protected project/provider/purpose/scope/audience forgery, lifecycle resurrection/rollback, retirement acknowledgement loss, operation acknowledgement loss/reconciliation, conflicting operation reuse, monotonic concurrent leases, restart, audit counters, repr/non-leakage and provider substitution.

Independent #432 adds three current attack families without production edits:

1. raw material alone cannot bootstrap a caller-created first authority;
2. a peer adapter's opaque handle cannot remain redeemable after another adapter revokes/rotates protected lifecycle authority;
3. the process owner primitive must be cross-session and user-scoped.

The physical Windows proof additionally exercises real `WinVaultKeyring`, authority persistence/retirement, process-ephemeral handles, source and frozen PyInstaller execution, ctypes/cffi paths, 2560-byte material, cleanup and concurrent-process ownership/release.

## Security / secret truth

- No real API key, OAuth token, cookie, browser profile, password or account credential is committed or printed.
- Tests use synthetic credential material only.
- No OS credential enumeration API or raw-secret getter is introduced.
- No provider/cloud/account effect is executed by this batch.
- M10/R4 production authority is unchanged.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- QA_ONLY proof/audit vehicles must never be merged to production.

## Acceptance evidence rule

Only terminal Actions attached to the final exact PF7 production SHA count. Required before guarded integration:

- Core CI exact-head success;
- complete M12 exact-head success;
- real PF3 Windows Credential Manager proof on the same production SHA in both ctypes/cffi and frozen paths;
- independent #432 exact-parent replay success plus independent security classification;
- current AUD03/reliability replay for effect/acknowledgement/restart/concurrency boundaries;
- shared workflow supply-chain dependency closure and a final current-main compatibility reread;
- zero unresolved review blockers and expected-head guarded merge.

Historical green evidence is lineage only after the production head moves. `INTEGRATED=false` until those conditions are proven.
