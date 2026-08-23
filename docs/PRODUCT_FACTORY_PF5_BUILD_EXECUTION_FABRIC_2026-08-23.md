# PF5 Build Execution Fabric — 2026-08-23

## Status

MANUAL-DEV07 candidate for the Product Factory multi-platform build/execution gate.

- Original run start `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
- Refreshed live `main` used by this branch: `e40691a6e2ff9c31fd413f63d004612e048d95ed`.
- Branch: `work/manual-dev07/pf5-build-execution-fabric`.
- PR: `#177`.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- No real cloud, SSH, WinRM, provider, staging, production, or credential-secret action is performed.

## Collision decision

Active PF3 fleet replacement/rebalancing ownership remains outside MANUAL-DEV07. Current successor
PR #165 owns that production slice; historical PR #130 remains separate. MANUAL-DEV09 PR #172 owns
production promotion authorization. This candidate edits none of those production paths.

The batch stays on the independent PF5 build-execution surface and reuses the integrated
`ExecutionNodeRegistry`, `ExecutionRequest`, `WorkLease`, platform, capability, resource, and
`NormalizedBuildEvidence` contracts.

## Acceptance target

The binding PF5 gate requires:

1. explicit platform/capability/resource node selection;
2. multiple execution environments normalized through one evidence contract;
3. unavailable Windows/Linux/macOS/GPU capability must wait or route to an authorized node;
4. no fabricated successful evidence;
5. project-scoped path, network, credential, node, and command authority;
6. restart/uncertain external dispatch must never cause blind replay;
7. corrupted persisted state must fail closed.

## REUSE → ADAPT → CUSTOM(thin)

### REUSE

The candidate reuses:

- `ExecutionNodeRegistry` for deterministic matching and leases;
- `ExecutionRequest` for project/work/platform/features/toolchains/GPU/resources;
- `WorkLease` for capacity and expiry;
- `NormalizedBuildEvidence` for normalized result evidence;
- `Platform.WINDOWS`, `Platform.LINUX`, and `Platform.MACOS`;
- existing node feature/toolchain/GPU/resource matching;
- Product Factory's established host-owned permission-ceiling model as the source that a trusted
  composition-root adapter must use when implementing `TrustedExecutionAuthorityPort`.

No generic scheduler, remote-shell framework, cloud SDK, second credential store, or new dependency is
introduced.

### ADAPT

PF3 node contracts are adapted with:

- candidate **scope requests**, not candidate authority;
- host-resolved exact project/repository/work execution authority;
- `build_release` permission-ceiling enforcement;
- requested-node subset intersection;
- project-relative workspace containment;
- bounded network-scope and opaque `credref:` subset checks;
- host-approved typed build command IDs resolving to exact argv;
- live availability rerouting;
- explicit pre-effect and uncertain-effect states;
- normalized evidence and restart corruption validation.

### CUSTOM(thin)

Nika-specific thin contracts are:

- `BuildExecutionScopeRequest`;
- `ApprovedBuildCommand`;
- host-owned `ProjectExecutionAuthority`;
- `TrustedExecutionAuthorityPort`;
- `ExecutionGrant`;
- `BuildExecutionSpec`, dispatch/result/record/snapshot;
- `BuildExecutionCoordinator`;
- normalized `BuildExecutionPortError`;
- node availability/execution ports.

DEV27 remains owner of low-level process, workspace, shell, reparse-point, and OS containment. PF5 does
not claim those protections merely because its command authority is narrow.

## AUD02 authority repair

The original candidate embedded `ProjectExecutionAuthority` and arbitrary `argv` in caller-constructible
`BuildExecutionSpec`. AUD02 correctly classified that as `BLOCK`: candidate state was its own trust
anchor and could mint wider node/network/credential/workspace authority or select a generic shell.

The repaired contract removes both capabilities from candidate input.

A candidate can now request only:

- repository identity;
- project-relative workspace path;
- requested node IDs;
- requested network scopes;
- requested opaque credential refs;
- an opaque build `command_id`.

`BuildExecutionCoordinator` receives a `TrustedExecutionAuthorityPort` from the trusted composition
root. `submit()` resolves authority for the exact `(project_id, repository_id, work_id)` and rejects
mismatched identities. The resolved authority must contain `build_release` permission provenance and
independent evidence refs. Candidate scope is accepted only when every requested node/network/
credential/path value is inside that host-owned authority.

The trusted resolver is the integration boundary for the canonical ProductProject/team permission
ceiling. Candidate/job payloads cannot implement or replace it through `submit()`.

## Command authority

The candidate no longer supplies `argv`.

A trusted authority exposes `ApprovedBuildCommand(command_id, argv)` entries. The candidate selects only
a command ID already approved by the host. Dispatch carries the exact host-approved argv in the durable
`ExecutionGrant`.

Generic shell executables are rejected at the approved-command boundary, including `cmd.exe`,
PowerShell/pwsh, bash/sh/zsh/fish, WSL, and path-qualified forms of those executables. This is an
additional PF5 command-authority guard, not a replacement for DEV27 process containment.

## Project-scoped path/network/credential authority

Workspace paths normalize Windows `\\` separators to `/` and preserve Unicode, spaces, and case.
Drive-qualified, UNC/absolute, empty-segment, `.`, and `..` paths fail closed. Containment is checked by
path segments, so sibling prefixes such as `products/app-secret` cannot satisfy authority for
`products/app`.

Requested network scopes must be a subset of host authority and cannot contain wildcard `*` authority.
Credentials are opaque `credref:` identities only; raw passwords, tokens, OAuth material, cookies,
browser profiles, and provider sessions have no value-bearing field in this contract.

## Routing and unavailable-platform behavior

`prepare()` reuses `ExecutionNodeRegistry.acquire()`. The integrated registry remains authoritative for
platform, required features/toolchains, GPU requirement, CPU/memory/disk capacity, enabled state,
busy-node exclusion, and lease expiry.

A temporarily acquired candidate is retained only when its node is also inside the durable execution
grant and currently available. Skipped leases are released. If no authorized capable node exists,
work becomes `WAITING_FOR_NODE`; no node port is invoked and no success evidence exists.

This supports Windows/Linux/macOS identities, GPU matching, and traits such as `on-prem` without a
second platform/resource model.

## Authority revocation and TOCTOU

Trusted authority is re-resolved before capacity preparation, before dispatch, and immediately before
external node execution.

If authority changes before any external effect, the exact node lease is released and work becomes
`WAITING_FOR_AUTHORITY`. The node port is never called. Restoring a snapshot whose durable grant no
longer matches current host authority fails closed.

Once an external effect may already exist, later authority revocation must **not** erase its dispatch
identity. `DISPATCHING`, `EFFECT_IN_FLIGHT`, and `RECONCILE_REQUIRED` retain the exact prior dispatch so
inspection/reconciliation remains possible and duplicate work is not manufactured.

## Effect boundary and retry safety

States are:

- `PENDING`;
- `WAITING_FOR_NODE`;
- `WAITING_FOR_AUTHORITY`;
- `PREPARED`;
- `DISPATCHING`;
- `EFFECT_IN_FLIGHT`;
- `RECONCILE_REQUIRED`;
- `SUCCEEDED`;
- `FAILED`.

`begin_dispatch()` creates deterministic immutable dispatch identity. `run_dispatch()` moves to
`EFFECT_IN_FLIGHT` before calling the node port.

Expected transport/provider uncertainty must be normalized by an adapter as `BuildExecutionPortError`.
That becomes `RECONCILE_REQUIRED` and can only be resolved through `inspect()`.

Unexpected programming exceptions are deliberately **not** hidden by a broad exception handler. They
propagate, while the coordinator remains `EFFECT_IN_FLIGHT`. A second `run_dispatch()` is rejected; only
reconciliation may continue. This both satisfies Ruff `BLE001` and preserves duplicate-effect safety.

The coordinator exposes snapshot/restart semantics; a production composition must durably persist the
post-dispatch/effect boundary before relying on crash-recovery guarantees. This PR does not claim a new
SQLite persistence framework or a physical remote provider implementation.

## Evidence truth

The node result cannot supply project/work/node authority. Those identities come from the exact trusted
dispatch. A result must contain:

- exact lowercase 40-character source SHA;
- lowercase SHA-256 artifact digest;
- exact boolean success/uncertainty fields;
- non-empty evidence refs;
- timezone-aware completion time.

Source-SHA mismatch becomes `RECONCILE_REQUIRED`, never success. Definite results are converted to the
existing `NormalizedBuildEvidence` type; Windows and Linux use the same contract.

## Restart and corrupted-state behavior

Restore validates the durable grant against **fresh independent host authority**. A forged snapshot
cannot widen nodes, network scopes, credentials, workspace scope, command identity, argv, or authority
provenance merely by remaining internally self-consistent.

Active PREPARED/DISPATCHING/EFFECT_IN_FLIGHT records are cross-checked against exact registry lease ID,
node ID, node existence, platform, resources, features, toolchains, GPU requirement, and trusted grant.
A same-project/work substituted lease is corruption and fails closed. A genuinely missing/expired
PREPARED lease is recoverable capacity loss and returns to `WAITING_FOR_NODE`.

Restart across `DISPATCHING` or `EFFECT_IN_FLIGHT` releases matching capacity and transitions to
`RECONCILE_REQUIRED`, preserving exact dispatch identity for inspection-only recovery.

Strict persisted scalar validation rejects Python bool aliases for integer attempts/lease duration and
non-boolean status values. Forged dispatch IDs and inconsistent terminal evidence fail closed.

## Qualification

The prior exact head `ff30f63abbf03b061680b7bdce331c245d559235` was correctly RED:

- Core CI #1100: Windows and Ubuntu dependency consistency + exact checkout passed, Ruff failed;
- M12 #868: same source-gate failure family;
- exact Ruff findings were DEV07-owned `BLE001` and `PIE810` only;
- PF3 credential-store proof #332 skipped as expected.

The repaired local contract harness currently has **52 passing pytest parameter instances** covering:

- candidate authority/argv removal;
- exact host work binding and permission ceiling;
- node/network/credential/workspace/command anti-escalation;
- generic-shell command rejection;
- Windows/Linux normalized evidence;
- macOS unavailable, GPU/on-prem routing, busy capacity and node loss;
- authority revocation before dispatch and before node execution;
- post-effect authority revocation without dispatch loss;
- typed transport uncertainty and unexpected-adapter-exception no-replay behavior;
- restart inspection-only behavior;
- forged durable grant/evidence/dispatch rejection;
- substituted lease/node and capability drift;
- strict integer/boolean identities.

Local authoring checks on the repaired files:

- Python `py_compile`: PASS;
- focused semantic pytest: `52 passed`;
- line-length <= 100: PASS;
- local Ruff executable remains unavailable, so **no local Ruff GREEN is claimed**.

Only a fresh exact GitHub candidate head after this repair may receive acceptance credit.

## Explicitly unverified

This candidate does not claim:

- physical macOS/Xcode execution;
- remote Linux execution;
- isolated physical Windows execution;
- a real GPU/on-prem node;
- SSH/WinRM/cloud/provider calls;
- real credential resolution/use;
- a concrete durable PF5 SQLite checkpoint host for every transition;
- production deployment/promotion;
- human NVDA verification.

Integration remains TECH02-owned; MANUAL-DEV07 does not self-merge.
