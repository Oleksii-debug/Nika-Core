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
- Bundle and destination trees may not overlap.
- Install is staged in a sibling directory on the destination volume, then renamed into place.
- Update verifies the current installed image, stages and verifies the new image, retains one sibling rollback image, and restores the old image if activation fails.
- Rollback swaps the installed image with the verified rollback image, allowing the superseded version to remain available as the next rollback target.
- The installer never requests elevation and never mutates the canonical Nika user-data directory. User data remains outside the application installation tree.

## Evidence boundary

This package proves the reusable install/update/rollback authority and a real Windows filesystem journey in Core CI. It does **not** by itself make a final V0.1 release candidate installable: after the active M11/package lane settles, the exact installer script still has to be bound into the final release artifact and rerun through the combined package/UIA/recovery gate.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`PRODUCTION_RELEASE_READY=false`
