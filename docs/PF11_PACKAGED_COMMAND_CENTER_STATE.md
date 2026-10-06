# PF11 packaged ProductCommandCenter state

Status: automated candidate evidence only. `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `PRODUCTION_RELEASE_READY=false`.

## Technical baseline

This slice starts from exact integrated `main` `42e8485060cb8189210f93f44de2e97e148f49de`, which contains both the PF5 ProductProject Command Center merge and the DEV-B packaged ProductProject routing merge. GitHub is technical truth; the Drive Product Factory ownership map remains routing/ownership truth.

## REUSE → ADAPT → CUSTOM(thin)

**REUSE** the integrated `ProductCommandCenter`, `ProductProjectCommandService`, deterministic `route_command`, `PackagedProductCommandRouter`, `UIActionBridge`, canonical SQLite persistence, M11 PyInstaller release builder, release manifest, and M12 exact-distributable binding.

**ADAPT** only the packaged Windows composition root and automated PF11 proof so the existing bridge state can consume the integrated ProductCommandCenter projection after a product command.

**CUSTOM(thin)** is limited to `PackagedProductStateProvider` plus a process-local active ProductProject pointer. The pointer is presentation state only and is never a durable authority record. There is no second ProductProject store, active-project table, scheduler, state machine, provider adapter, credential store, UI shell, SSH/WinRM path, or cloud control plane.

## Product and security behavior

A successful product command selects the exact durable ProductProject for the current packaged process. Replaying the same normalized product command after process restart deterministically reopens and re-selects the same durable project through the existing idempotent PF1/PF5 path.

Ordinary AgentTask commands do not create/select ProductProjects. Ambiguous ProductProject+Toolsmith and Toolsmith-only commands remain fail-closed and cannot replace the last valid ProductProject selection.

The state provider reads the selected project through the integrated `ProductCommandCenter`, not directly from PF3 internals. It returns only bounded presentation fields: project identity, spec version, title, goal, lifecycle state, blocker/status counts and decision counts. It does not serialize evidence references, credential references, authorization references, provider sessions, protected-store handles, worker leases or deployment credentials.

If PF5 detects that durable ProductProject state changed while one presentation snapshot was being composed, the packaged provider converts that inconsistency into a controlled refresh-required rejection; it never returns a mixed stale snapshot.

No shared WebView HTML/JavaScript/CSS, DEV04 UIA source, manual DEV01–DEV05 source, M10/security source, PF2 coordinator internals, PF3 deployment internals, provider implementation or credential-store implementation is changed by this batch.

## Automated qualification

Focused tests cover durable create/replay, process-local selection reset, bounded ProductCommandCenter state, ordinary/ambiguous/Toolsmith isolation, last-valid-selection preservation, whitespace-stable identity, concurrent-read fail-closed behavior and a 60-ProductProject deterministic identity/state fixture.

The physical PF11 proof runs the packaged `NikaCore.exe --pf11-proof` twice against the same temporary SQLite database. Each process must recreate the same command route, reopen the same ProductProject at spec version 1, obtain the same ProductCommandCenter-backed bridge projection and emit identical bounded evidence. M11 rejects malformed counts, identity mismatch, changed restart output, missing CommandCenter proof, or any attempt to claim human/NVDA/production readiness.

The resulting `pf11-packaged-product-journey.json` is written before release-manifest construction, so its exact bytes are manifest-bound. M12 separately binds the final outer distributable ZIP to exact source SHA, size and SHA-256.

## Real versus simulated

The packaged composition, ProductProject repository/service, ProductCommandCenter, SQLite persistence, bridge state-provider and release builder are production code. Focused unit tests use local temporary SQLite and controlled fake subprocess output only where the M11 validator itself is isolated. GitHub Windows packaging is the authority for the real PyInstaller executable proof.

No real cloud/provider action, remote deployment, SSH/WinRM, production account action or real credential is used by this qualification.
