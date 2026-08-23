# PF5 Build Execution Fabric — 2026-08-23

## Status

MANUAL-DEV07 candidate for the Product Factory multi-platform build/execution gate.

- Starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
- Branch: `work/manual-dev07/pf5-build-execution-fabric`.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- No real cloud, SSH, WinRM, provider, staging, production, or credential-secret action is performed.

## Collision decision

Live Issue #1 and open PR #130 establish an active PF3 writer for fleet replacement/rebalancing.
MANUAL-DEV07 therefore does not edit or duplicate `product_factory_fleet_replacement.py`,
`product_factory_fleet_maintenance.py`, or their replacement/provider coordination.

This batch stays below that layer and adds a build-execution coordinator over the already-integrated
`ExecutionNodeRegistry`, `ExecutionRequest`, `WorkLease`, platform, capability, and resource contracts.
It is intentionally additive so the active fleet owner can integrate independently.

## Acceptance target

The binding PF5 gate requires:

1. explicit capability-based execution-node selection;
2. at least two distinct execution environments returning normalized evidence through one contract;
3. unavailable Windows/Linux/macOS/GPU capability must fail clearly or route to an authorized node;
4. no fabricated successful evidence;
5. project-scoped paths, network authority, and credential references only;
6. restart/uncertain external dispatch must not blindly replay side effects.

## REUSE → ADAPT → CUSTOM(thin)

### REUSE

The candidate reuses:

- `ExecutionNodeRegistry` for deterministic matching and lease ownership;
- `ExecutionRequest` for project/work/platform/features/toolchains/GPU/resources;
- `WorkLease` for capacity and expiry;
- `NormalizedBuildEvidence` for normalized build-result evidence;
- `Platform` for Windows, Linux, and macOS;
- `NodeCapabilities.features` for traits such as `on-prem`;
- `NodeCapabilities.gpu` plus `ExecutionRequest.require_gpu` for GPU routing.

No generic scheduler, remote shell framework, cloud SDK, credential store, or persistence framework is
reimplemented.

### ADAPT

The integrated registry is adapted with:

- project-specific authorized-node allowlists;
- portable project-relative workspace authority;
- explicit network scopes and opaque `credref:` references;
- live node availability filtering;
- rerouting past unavailable or unauthorized matching nodes;
- exact pre-dispatch/post-dispatch state transitions;
- normalized result validation;
- restart cross-validation against live registry leases.

### CUSTOM(thin)

Only Nika-specific orchestration/trust contracts are custom:

- `ProjectExecutionAuthority`;
- `BuildExecutionSpec`;
- `BuildExecutionDispatch`;
- `BuildExecutionResult`;
- `BuildExecutionRecord` / `BuildExecutionSnapshot`;
- `BuildExecutionCoordinator`;
- `BuildExecutionNodePort` and `ExecutionNodeAvailabilityPort`.

A Windows worker, Linux CI executor, macOS/Xcode node, GPU worker, or on-prem worker can implement the
same port later without changing the durable coordinator contract.

## Project-scoped authority

Every work item carries one `ProjectExecutionAuthority` containing:

- `project_id`;
- `repository_id`;
- normalized project-relative workspace path;
- explicit non-empty authorized-node set;
- exact network scopes;
- opaque `credref:` references only.

Workspace paths accept Windows separators and normalize to `/`, including Unicode/Cyrillic names and
spaces. Drive-qualified, UNC, absolute, empty-segment, `.`, and `..` paths fail closed.

Wildcard network authority is rejected. Raw passwords, tokens, cookies, API keys, OAuth payloads,
browser profiles, and provider sessions have no field in the contract.

## Routing behavior

`prepare()` reuses the base allocator. It may temporarily lease and skip candidates that are outside
the project-authorized node set or currently unavailable according to `ExecutionNodeAvailabilityPort`.
Skipped leases are released before `prepare()` returns. The first matching authorized live candidate is
retained.

The base registry remains authoritative for platform, features, toolchains, GPU requirement,
CPU/memory/disk envelope, enabled/cordoned state, busy-node exclusion, and lease expiry.

If no suitable node exists, the durable record becomes `WAITING_FOR_NODE` with a platform/GPU reason.
No node port is called and no successful evidence can exist.

## Platform/capability identity

The existing public contracts already express:

- Windows: `Platform.WINDOWS`;
- Linux: `Platform.LINUX`;
- macOS: `Platform.MACOS`;
- GPU: `gpu=True` plus `require_gpu=True`;
- on-prem: an explicit required feature such as `on-prem`;
- toolchain identity through `required_toolchains`;
- resource capacity through `ResourceEnvelope`.

The candidate does not introduce competing platform/resource enums.

## Durable state machine

States:

- `PENDING` — accepted, no node lease;
- `WAITING_FOR_NODE` — no authorized/live/capable capacity;
- `PREPARED` — exact node/lease selected, no external side effect;
- `DISPATCHING` — immutable dispatch identity created before external execution;
- `RECONCILE_REQUIRED` — outcome may have happened and must be inspected;
- `SUCCEEDED` — definite success with normalized evidence;
- `FAILED` — definite failure with normalized evidence.

`begin_dispatch()` creates the explicit persistence boundary. A caller can persist the coordinator
snapshot before invoking the external node port.

## Node loss and uncertainty

Immediately before dispatch, availability is checked again. Node loss releases the lease, returns the
work to `WAITING_FOR_NODE`, and performs no external run.

After the dispatch boundary, a node-port exception or uncertain result never becomes a safe retry.
The work becomes `RECONCILE_REQUIRED`, capacity is released, and only `inspect()` may resolve the exact
prior dispatch. This prevents duplicate external work after process or transport failure.

## Evidence truth

The node port result does not provide `node_id`, `work_id`, or project identity. Those values are taken
from the immutable coordinator dispatch, preventing a response from attributing success to another
node/work item.

A result must contain an exact lowercase 40-character source SHA, lowercase SHA-256 artifact digest,
success/uncertainty truth, non-empty evidence references, and timezone-aware completion time.

A source-SHA mismatch becomes `RECONCILE_REQUIRED`; it never becomes success. Definite results are
converted to the existing `NormalizedBuildEvidence` contract. Windows and Linux qualification uses
that same evidence type.

## Restart and corrupted-state behavior

Coordinator snapshots contain durable records; the base registry owns leases. Restore cross-validates
both views.

For `PREPARED`, the exact project/work/lease/node identity must still exist and be unexpired. Otherwise
work safely returns to `WAITING_FOR_NODE`.

For `DISPATCHING`, restart releases matching capacity and converts to `RECONCILE_REQUIRED`, preserving
the exact dispatch for inspection rather than replaying `run()`.

Restore rejects duplicate work IDs, duplicate registry leases for one project/work, dispatch/spec drift,
pre-dispatch records containing dispatch identity, active records without exact node/lease identity,
post-dispatch records without exact dispatch, terminal records without normalized evidence, forged
work/node/source/success evidence, and non-terminal records claiming terminal evidence.

## Idempotency and concurrency

`submit()` is idempotent for an identical `BuildExecutionSpec`. The same work ID with changed payload is
rejected. Concurrency remains owned by the integrated execution-node lease contract; no parallel lease
system is created.

## Focused deterministic qualification

The candidate focused suite contains 23 pytest cases/parameter instances covering:

- Unicode/Windows path normalization;
- traversal, drive, UNC, raw-credential, and wildcard-network rejection;
- Windows + Linux normalized evidence through one contract;
- macOS unavailable fail-closed behavior;
- GPU routing;
- on-prem feature routing plus project node authorization;
- reroute around an unavailable matching node;
- busy-capacity wait/retry;
- node loss before dispatch;
- uncertain result and node-port exception reconciliation without second `run()`;
- PREPARED restart with exact unexpired lease;
- DISPATCHING restart to inspection-only recovery;
- expired lease restart;
- forged terminal evidence rejection;
- duplicate-submit idempotency and changed-payload rejection;
- wrong source SHA rejection without success/capacity leak;
- duplicate active registry lease rejection;
- forged pre-dispatch dispatch-state rejection.

All execution tests use deterministic fake node availability and fake node ports. No remote/provider
side effect occurs.

## Local authoring preflight truth

The authoring container cannot resolve `github.com`, so a canonical local checkout and repository Ruff
binary are unavailable. No local full-repository GREEN or local Ruff GREEN is claimed.

Completed before publication:

- production module `py_compile`: PASS;
- focused test module `py_compile`: PASS;
- focused semantic pytest harness against the visible integrated PF3 contract shapes: `23 passed`;
- source/test maximum line length <= 100: PASS.

## Exact-head acceptance still required

After publication, acceptance credit requires one exact candidate SHA with repository CI proving
Ubuntu + Windows, dependency consistency, Ruff/format, compile/import, relevant/full tests, and M12
when triggered. A final live-main reread and collision check are also required. MANUAL-DEV07 does not
self-merge; integration belongs to TECH02.

## Explicitly unverified

This candidate does not claim a real macOS/Xcode worker, remote Linux CI worker, isolated Windows
worker, GPU host, on-prem transport, cloud/provider credentials, SSH/WinRM/Ansible/Kubernetes action,
production deployment, human NVDA test, or production release approval.
