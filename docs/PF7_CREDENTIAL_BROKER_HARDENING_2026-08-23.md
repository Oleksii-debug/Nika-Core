# PF7 Credential Broker hardening — 2026-08-23

Status: MANUAL-DEV08 implementation/evidence record. This document does not grant integration, human-testing or NVDA credit.

## Exact lineage

- Repository: `Oleksii-debug/Nika-Core`.
- Starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
- Lane: `MANUAL-DEV08` / PF7 Credential & Identity Broker.
- Branch: `work/manual-dev08/pf7-project-bound-protected-store`.
- PR: #162.

## REUSE -> ADAPT -> CUSTOM (thin)

**REUSE:** retain the existing maintained Python `keyring` Windows Credential Manager integration and the existing explicit `WinVaultKeyring` adapter documented in `docs/PRODUCT_FACTORY_CREDENTIAL_STORAGE.md`. No custom cryptographic vault, plaintext fallback or new credential dependency is introduced.

**ADAPT:** keep `CredentialBroker` and `ProtectedSecretStorePort` as framework-neutral Nika contracts. OS-backed material remains behind the protected-store boundary; workers and ProductProject state receive only reference metadata and opaque lease/handle authority.

**CUSTOM (thin):** add only Nika-owned PF7 policy/integrity checks that an OS credential backend cannot know: exact project-scoped reference resolution, no public broker enumeration surface, strict integer identity validation, fail-closed restart validation against protected-store availability, and representation hardening so the broker never delegates ordinary `repr()` to a protected-store object.

## Hardened invariants

1. **No public secret enumeration:** `CredentialBroker` exposes exact `get_secret_ref(project_id=..., secret_ref=...)` resolution only. A caller must already possess the opaque reference; unrelated projects receive the same unavailable-reference failure.
2. **Restart fails closed for active material:** `restore()` validates that every `ACTIVE` `SecretRef` generation still exists in protected storage before accepting snapshot state. A missing active generation rejects restore without installing the snapshot.
3. **Revoked history remains recoverable:** a `REVOKED` reference may restore even if its old OS material has already been physically retired. This preserves audit/history without restoring authority.
4. **Lease authority is still process-ephemeral:** broker snapshots do not persist active leases or protected-store handles. Restart requires fresh lease issuance against currently available protected material.
5. **Strict numeric identities:** credential generation and lease TTL fields require positive Python integers and explicitly reject boolean aliases. The same rule applies to `SecretRef`, `CredentialLease` and `CredentialUseEvidence` generation identities.
6. **Representation boundary:** the protected store field is excluded from `CredentialBroker.__repr__`. This prevents an alternate/future store implementation with an unsafe `repr()` from leaking material into ordinary logs, exception context or model-prompt serialization of the broker object.
7. **Existing attenuation remains authoritative:** audience and scopes must be subsets of the registered reference policy; expiration, revocation and rotation invalidate later use as already defined by the integrated broker.
8. **Audit remains value-free:** credential use evidence and broker audit records carry reference metadata only; no raw API key/password/token is introduced by this batch.

## Adversarial qualification in this branch

Focused regressions cover:

- exact project-scoped lookup and absence of the old enumeration method;
- cross-project reference substitution rejection;
- missing active protected generation on restart;
- revoked-history restart positive control;
- Python `bool` substitution for generation and TTL;
- dangerous protected-store `repr()` containing fake raw material;
- existing expiry, revoke, rotation, scope/audience attenuation, identity isolation, snapshot tamper, audit monotonicity and raw-secret non-serialization suites remain in the repository and are rerun by Core/M12.

## Deliberate compatibility boundary

This batch does **not** change the existing Windows Credential Manager target format (`NikaCore.ProductFactory.v1.<sha256(secret_ref)>.g<generation>`), physically migrate stored credentials, expose a raw-secret resolver, add a provider SDK, or alter M10 authorization/approval source. A future target-format migration, if required, must be versioned and crash/restart tested rather than silently changing existing OS credential identities.

## Security / secret truth

- No real credential, API key, OAuth token, cookie, browser profile or password was used or committed.
- Test strings are synthetic non-secret fixtures only.
- No credential enumeration of the operating-system vault was added.
- No provider/cloud/account action is executed by this batch.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- Integration is owned by TECH02; MANUAL-DEV08 does not self-merge.

## Acceptance evidence rule

Only GitHub Actions attached to the final exact PR head receive GREEN credit. Superseded/cancelled runs do not transfer evidence. Core CI must prove dependency consistency, Ruff/format/static checks, compile/import and the full test suite on its configured platforms; M12 must independently succeed on the same exact head. The focused Windows credential-store workflow may legitimately skip when its path filter excludes a broker-only change; no physical Windows Credential Manager proof is newly claimed unless that workflow actually runs on the exact candidate.
