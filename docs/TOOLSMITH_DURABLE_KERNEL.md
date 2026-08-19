# Toolsmith durable kernel — DEV02 Batch 1

Updated: 2026-08-19.

Status: implementation candidate. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Scope

This batch adds the durable domain/persistence kernel for capability escalation. It does not add
Codex, OpenHands, a shell wrapper, production GitHub writes, shared UI, or an execution sandbox.

The implemented flow is:

`running task -> CapabilityGap -> deterministic classification -> reuse/build decision ->
BUILDING/verification -> exact registration -> same-task resume binding`.

The original `task_id` is retained through the escalation record and the resume binding. A
registered capability is pinned by exact `capability_id + version + digest`; registration rejects
a version collision if the same version already exists with a different digest.

## Security truth

`AllowedPathPolicy` is **POLICY_ONLY**. It rejects absolute/drive paths, parent traversal,
case-insensitive `.git`, alternate-data-stream style `:` path parts, and repository-relative scope
escape. That does not make a worktree a filesystem sandbox.

`ProcessPolicy` and `AcceptanceCommand` are typed argv policy contracts. Generic shell execution
is rejected; Batch 1 itself executes no subprocess.

`NetworkPolicy` defaults to `DENY`. Batch 1 itself performs no worker network operation.

The deterministic fake worker exists only for tests and cannot execute commands or network
requests. No production remote, GitHub token, browser profile, SSH credential, API key, or other
secret is passed to it.

Untrusted candidate execution is intentionally not claimed in Batch 1. That requires a later
`OS_SANDBOXED` or `REMOTE_SANDBOXED` verifier. A worktree alone is not a security boundary and a
Windows Job Object, when added later, proves process containment rather than filesystem/network
sandboxing.

## Deterministic classifier

Only `MISSING_CAPABILITY` with prior search evidence and a non-empty original permission ceiling
may proceed toward build. These categories are fail-closed and cannot trigger code construction:

- `MISSING_INFORMATION`;
- `AMBIGUOUS_GOAL`;
- `TOOL_FAILED`;
- `MODEL_FAILED`;
- `PERMISSION_DENIED`.

`EXISTING_CAPABILITY_AVAILABLE` routes to reuse.

## Durable state

Ordered SQLite migration 8 adds:

- `capability_escalations` with optimistic `row_version`;
- `capability_search_candidates`;
- exact-version `capability_registry`;
- `capability_resume_bindings`.

State transitions and their audit event are committed in the same caller-owned SQLite
transaction using `AuditLog.append_with_connection`. Stale row versions and illegal edges fail
closed.

The state graph is:

`PROPOSED -> REUSE_SELECTED / BUILD_REQUIRED / BLOCKED`

`REUSE_SELECTED -> VERIFYING -> VERIFIED -> REGISTERING -> REGISTERED`

`BUILD_REQUIRED -> BUILDING -> BUILT -> VERIFYING -> VERIFIED -> REGISTERING -> REGISTERED`

with explicit `REJECTED`, `BLOCKED`, `QUARANTINED`, and `ROLLED_BACK` failure/rollback states.

A blocked escalation writes the existing task checkpoint first and only then persists BLOCKED.
This preserves the original task context before escalation stops.

If the process disappears after BUILDING is durable, `recover_build()` requires both the exact
durable row version and worker recovery state. Missing recovery state blocks instead of replaying
the build blindly. This is the Batch 1 duplicate-build guardrail.

## Verification boundary

Coding-worker `TestEvidence` is evidence returned by the worker, not authoritative verification.
`VERIFIED` requires a separate verifier evidence payload and exact candidate digest supplied by
Nika-side verification logic. Batch 1 defines/persists this boundary; it does not yet execute an
untrusted candidate in an independent sandbox.

## Reuse/search ordering

The capability search contract records source metadata so the caller can preserve the binding
search order:

1. Tool Registry;
2. Plugin Registry;
3. MCP metadata;
4. workspace capabilities;
5. installed distribution metadata;
6. approved catalog.

Candidates whose permissions are not a subset of the original task/gap permission ceiling are
rejected before selection or registration.

## Dependencies and licensing

No new runtime dependency is introduced. This batch reuses Python standard-library types and the
existing Nika `SQLiteStore`, ordered migration mechanism, `AuditLog`, and `CheckpointService`.
No third-party coding-agent dependency is adopted in Batch 1.

## What remains unproven / next large batch

Batch 2 must establish job-private Git metadata, remove production `.git`/remotes/credential
helpers from worker view, harden Windows path/reparse/UNC/device/ADS handling with physical tests,
add typed `shell=False` process execution plus Windows Job Object containment, cancellation and
process-tree recovery, collect authoritative tree/diff evidence, prove production-main integrity,
and then add a thin Codex adapter if its current official Windows/API/license surface passes the
reuse gate.
