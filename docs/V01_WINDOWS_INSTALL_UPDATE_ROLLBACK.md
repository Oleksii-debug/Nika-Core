# V0.1 Windows install, update and rollback contract

This is a thin release-layer adapter for the existing Nika Core one-directory Windows package. It does not create a second package format or replace the canonical release manifest.

## Reuse decision

The implementation adapts the already proven PF11 C1 non-admin PowerShell installation pattern: literal-path filesystem operations, Unicode/space paths, no elevation, system-directory refusal, and an installed executable proof. Core additionally treats the existing `release-manifest.json` as package authority before any destination mutation.

No WiX, Inno Setup, NSIS, MSIX or other installer dependency is introduced.

## Contract

- Default install target is `%LOCALAPPDATA%\Programs\NikaCore`.
- A caller may select another non-system destination.
- Install and update verify manifest version, product, source SHA, file identity, byte size and SHA-256 before changing the installed tree.
- The manifest must bind `NikaCore.exe`; unexpected files, duplicate Windows path identities, unsafe relative paths and reparse-point files fail closed.
- Every path component rejects trailing periods and spaces before filesystem lookup, matching the canonical release manifest and [Windows naming rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file).
- Bundle and destination trees may not overlap.
- Install is staged in a sibling directory on the destination volume, then renamed into place.
- Update verifies the current installed image, stages and verifies the new image, retains one sibling rollback image, and restores the old image if activation fails.
- Rollback swaps the installed image with the verified rollback image, allowing the superseded version to remain available as the next rollback target.
- Before the first destructive rollback rename, the installer persists a sibling `rollback-operation.json` acknowledgement that is bound to the exact verified pre-command destination and rollback manifest digests. A caller that can replay a rollback should pass one stable `-RollbackOperationId` in canonical 32-character lowercase hexadecimal form.
- Repeating the same rollback operation ID after a crash is idempotent: if the durable marker proves that the exact source/target pair is already reversed, the retry succeeds without another swap; if the exact pre-command pair remains, the same operation may resume. A mismatched ID/pair, malformed marker, non-file/reparse marker, or ambiguous state fails closed before another rollback mutation.
- A distinct rollback operation ID may supersede an older marker only after that marker is proven terminal against the current verified reverse pair. Omitting `-RollbackOperationId` is supported for the first operation and for retry compatibility, but once a terminal marker exists an intentional new reverse rollback requires a fresh explicit operation ID.
- Update may retire a rollback-operation marker only after verifying both the incoming update bundle and that the marker is terminal against the current destination/rollback pair; it may not supersede an in-flight or mismatched rollback operation.
- The installer never requests elevation and never mutates the canonical Nika user-data directory. User data remains outside the application installation tree.
- The canonical data root is resolved once per invocation and its existing ancestor chain must contain no reparse point/junction; lexical aliases are not trusted as separation authority.
- Before destructive filesystem changes, the data root is checked for two-way non-overlap with the application destination, deterministic rollback sibling, durable rollback-operation marker, and any currently allocated installer staging/recovery sibling paths; the boundary is rechecked immediately before rename/removal operations.

## Evidence boundary

This package proves the reusable install/update/rollback authority and a real Windows filesystem journey in Core CI. It does **not** by itself make a final V0.1 release candidate installable: after the active M11/package lane settles, the exact installer script still has to be bound into the final release artifact and rerun through the combined package/UIA/recovery gate.

The post-activation fault regression targets Install and Update separately. It requires exactly one matching injection site, positively verifies the injected junction, writes an independent marker, and observes the production reparse rejection. Failed Install leaves no active destination; failed Update restores the prior image and preserves external data. A missing or misdirected fault injection cannot count as recovery evidence. The rollback-operation regression separately injects a hard stop after the terminal swap but before caller acknowledgement, then proves that the same operation ID does not reverse the images again and that a fresh ID performs exactly one legitimate reverse rollback. These filesystem cases require actual Windows execution; Linux skips do not grant that evidence.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`PRODUCTION_RELEASE_READY=false`
