# Product Factory Windows credential storage

Status: PF3 implementation decision and acceptance evidence plan.
Updated: 2026-08-20.
Scope: Windows OS-backed protected storage behind the integrated Product Factory Credential Broker.

## Decision

Nika will **REUSE / ADAPT** the maintained Python `keyring` Windows backend rather than
implementing raw Win32 credential marshalling itself.

Adopted component set for this boundary:

| Component | Version | License | Role | Distribution SHA-256 |
| --- | --- | --- | --- | --- |
| `keyring` | 25.7.0 | MIT | Windows Credential Manager adapter | wheel `be4a0b195f149690c166e850609a477c532ddbfbaed96a404d4e43f8d5e2689f` |
| `pywin32-ctypes` | 0.2.3 | BSD-3-Clause | Win32 credential API shim preferred by keyring | wheel `8a1513379d709975552d202d942d9837758905c8d01eb82b8bcc30918929e7b8` |

Both are Windows-only optional Product Factory credential dependencies. The generic Broker
contracts remain framework-neutral and do not import keyring types.

Primary upstream evidence:

- PyPI `keyring` 25.7.0: https://pypi.org/project/keyring/25.7.0/
- upstream Windows backend:
  https://github.com/jaraco/keyring/blob/main/keyring/backends/Windows.py
- upstream dependency declaration:
  https://github.com/jaraco/keyring/blob/main/pyproject.toml
- PyPI `pywin32-ctypes` 0.2.3:
  https://pypi.org/project/pywin32-ctypes/0.2.3/
- Microsoft `CredWriteW`:
  https://learn.microsoft.com/windows/win32/api/wincred/nf-wincred-credwritew
- Microsoft `CredReadW`:
  https://learn.microsoft.com/windows/win32/api/wincred/nf-wincred-credreadw
- Microsoft `CredDeleteW`:
  https://learn.microsoft.com/windows/win32/api/wincred/nf-wincred-creddeletew
- Microsoft `CREDENTIALW`:
  https://learn.microsoft.com/windows/win32/api/wincred/ns-wincred-credentialw

## Why the adapter is explicit instead of keyring auto-selection

`keyring` supports user configuration, environment-selected backends and third-party backend
entry points. Those features are useful for general applications but are too broad for this
security boundary. Nika therefore does **not** call the top-level auto-selected
`keyring.get_keyring()`, `keyring.set_password()` or `keyring.get_password()` APIs.

`create_windows_credential_store()` imports `keyring.backends.Windows.WinVaultKeyring`
directly, verifies that backend is available, and sets persistence to `local machine`.
This prevents a user keyring configuration or a third-party backend entry point from silently
changing the Product Factory storage class.

Microsoft documents `CRED_PERSIST_LOCAL_MACHINE` as surviving later logons of the same user on
the same computer without making the credential visible to that user's sessions on other
computers. The keyring Windows backend defaults to enterprise persistence, so Nika overrides
that default deliberately to avoid roaming a Product Factory secret.

## Threat model and invariants

The adapter assumes the Windows account running Nika is trusted to own its own Credential
Manager vault. It protects against accidental plaintext persistence in ProductProject state,
Git, prompts, ordinary logs, snapshots and worker handoffs. It is not a defense against a
fully compromised Windows user session that can already call the same operating-system APIs.

Binding invariants:

1. Raw material enters only `provision_secret()` and the internal backend call. There is no
   public raw-secret getter and no credential enumeration method.
2. `ProtectedSecretStorePort` remains reference/handle-only. Broker and workers receive opaque
   handle strings, not passwords or API keys.
3. Windows target names are `NikaCore.ProductFactory.v1.<sha256(secret_ref)>.g<generation>`.
   The human-readable secret reference is not written as the Credential Manager target.
4. A fixed Nika username is used. Nika never requests `get_credential()` and never uses a null
   or caller-controlled Windows username.
5. A generation is write-once inside one Nika process. Re-provisioning the exact same material
   is idempotent; different material at the same generation fails closed and requires rotation.
6. Handle identity binds secret reference, generation, ProductProject, audience, scopes and
   expiry. Handles are random, process-ephemeral and excluded from dataclass/store repr.
7. Restart retains OS material but does not restore handle authority. New handles must be
   issued after the Broker and store are reconstructed.
8. Backend exceptions are reclassified using only operation and exception type. Their original
   message is not propagated into ordinary Nika error text because providers can include
   sensitive values in exception messages.
9. Credential material containing NUL is rejected. Generic Credential Manager blobs are
   bounded before write to Microsoft's 2560-byte limit using UTF-16LE byte length.
10. All handle/provision/delete mutations are protected by an in-process reentrant lock.

## Upstream hazards addressed explicitly

### Duplicate/update behavior

The Windows keyring backend intentionally moves an existing service credential to a compound
`username@service` target before each overwrite. Upstream issue #545 remains open and documents
surprising duplicate behavior for repeated updates.

Nika avoids that path: it reads the exact deterministic target first and never calls
`set_password()` when the generation already exists. Exact idempotent retries are no-ops;
different material is rejected. Rotation uses a new generation and therefore a new target.

### Username matching

The Windows vault target name is case-insensitive and keyring has had Windows username edge
cases. Nika never accepts a credential-store username from a ProductProject or worker. It uses
one fixed internal username and calls `get_password(service, username)` rather than
`get_credential()`.

### Credential size

Microsoft defines `CRED_MAX_CREDENTIAL_BLOB_SIZE` as `5*512` bytes. Upstream keyring issue #540
also records the practical Windows limit for long tokens. Nika checks encoded byte length before
calling the backend. The dedicated Windows proof writes and reads a generated 2560-byte test
credential and unit tests reject 2562 bytes before the backend is touched.

### PyInstaller backend discovery

Historical keyring issues #439 and #468 show that frozen executables could fail when relying on
dynamic backend discovery. Nika imports the Windows backend class directly and adds a dedicated
PyInstaller proof that executes the physical Credential Manager lifecycle from the frozen EXE.
The proof runs against both `pywin32-ctypes` internal binding paths: ctypes fallback and cffi.

## Lifecycle

### Provision

1. Host receives a user/provider credential through a future trusted credential-entry path.
2. Host selects an unused integer generation.
3. Adapter validates text, NUL absence and Windows byte limit.
4. Adapter reads only the deterministic target.
5. Missing target is written once. Exact retry is a no-op. Different material fails closed.
6. Only after storage succeeds may `CredentialBroker.register_secret()` persist the opaque
   `SecretRef` metadata.

There is deliberately no API that creates a `SecretRef` by scraping or enumerating Windows
Credential Manager.

### Lease and handle

The integrated Broker owns project, audience, scope and TTL policy. `issue_lease()` calls the
store's `issue_handle()` only after Broker attenuation checks. The Windows store independently
requires the referenced generation to exist and records an in-process handle binding.
`validate_handle()` returns reference-only evidence and never raw material.

A future real provider connector will require a separate trusted host-only redemption boundary.
That capability is **not** added in this batch because exposing a general resolver on the Broker
or worker surface would defeat the no-raw-secret contract.

### Revoke and rotate

Broker revocation/rotation invalidates all process handles for the affected generation through
`revoke_handles()`. The store also exposes explicit `delete_secret()` for physical cleanup.
Physical deletion is intentionally not hidden inside the current Broker state transition: the
Broker foundation is not yet backed by a transactional durable credential-state repository, so
mixing irreversible OS deletion into an in-memory metadata transition would create an unsafe
crash window.

The later durable credential lifecycle must order durable metadata transition and physical
retirement explicitly and include crash-before/crash-after recovery tests.

## Restart and recovery truth

OS material and Broker metadata have different authority lifetimes:

- Windows Credential Manager material persists for the same user on the same local machine.
- Broker snapshots contain only `SecretRef`, `IdentityRef` and audit-safe reference metadata.
- Broker active leases are intentionally not restored.
- Windows store handle mappings are intentionally not restored.
- After restart, the protected generation must still exist before a new lease can be issued.

The physical Windows proof creates a random temporary credential, reconstructs the adapter,
proves the same generation is still present and idempotently matches, proves the old handle did
not survive, then deletes every test generation in `finally`-style cleanup. It never prints the
secret, target or secret reference.

## Concurrency and crash limitations

An in-process `RLock` prevents two Nika threads from racing the read-before-write generation
rule. Windows Credential Manager does not expose a compare-and-set primitive through keyring,
so two independent Nika processes provisioning the same generation concurrently are not claimed
to be atomic. Product Factory must maintain one credential-authority host per user profile or
add an explicit cross-process authority lock before multi-process provisioning is enabled.

Python strings are immutable; this adapter cannot guarantee cryptographic zeroization of a
secret after the backend call. It minimizes secret lifetime and serialization surfaces but does
not claim secure-memory semantics.

## Dedicated acceptance proof

`.github/workflows/pf3-windows-credential-store.yml` runs on Windows for this PF3 branch in two
modes:

- `ctypes`: explicitly removes cffi so `pywin32-ctypes` uses its ctypes fallback;
- `cffi`: installs the exact cffi test version and proves that binding path separately.

Each mode must:

1. verify the exact Git candidate checkout SHA;
2. install exact credential dependencies plus QA tooling;
3. pass `pip check`;
4. run Broker foundation + Windows-store unit/integration regressions;
5. create/read/restart/rotate-guard/delete real temporary Windows credentials;
6. prove the 2560-byte Windows blob boundary;
7. package the proof with PyInstaller;
8. execute the frozen EXE against Windows Credential Manager;
9. clean up temporary credentials.

Core CI and M12 remain required in addition to this focused gate. A focused PF3 proof cannot
award `HUMAN_TESTED`, `NVDA_VERIFIED`, PF11, real-provider or production-deployment credit.

## Explicit non-goals for this batch

- no GitHub/cloud/hosting credential is used;
- no broker/provider SDK or real external connector action is added;
- no credential is accepted from model output;
- no worker receives a raw secret;
- no OS credential enumeration is added;
- no generic cross-platform fallback to plaintext or encrypted files is added;
- no `keyrings.alt` or third-party backend is accepted;
- no automatic physical deletion is coupled to non-durable Broker transitions;
- no ProductProject/UI/M10/DEV lane source is modified;
- no claim of human or NVDA verification is made.
