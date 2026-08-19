# Toolsmith durable kernel — DEV02 Batch 1 + Batch 2 security substrate

Updated: 2026-08-19.

Status: implementation candidate. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Scope

Batch 1 adds the durable domain/persistence kernel for capability escalation. Batch 2 begins the
worker security substrate with job-private Git planning, sterile Git environment/config policy,
Windows path hardening, typed executable argv validation, deterministic tree evidence and
production-integrity comparison. No real Codex/OpenHands provider is added yet.

The implemented durable flow remains:

`running task -> CapabilityGap -> deterministic classification -> reuse/build decision ->
BUILDING/verification -> exact registration -> same-task resume binding`.

The original `task_id` is retained through the escalation record and the resume binding. A
registered capability is pinned by exact `capability_id + version + digest`; registration rejects
a version collision if the same version already exists with a different digest.

## Security truth

`AllowedPathPolicy` and the new `WorkspacePathPolicy` are **POLICY_ONLY**. They reject
absolute/drive/UNC paths, parent traversal, case-insensitive `.git`, alternate-data-stream style
`:` path parts, Windows reserved device names, and trailing-dot/space components. Guarded physical
path checks also refuse symbolic links and Windows reparse points when encountered. These checks
do not make a worktree a filesystem sandbox.

`ProcessPolicy`, `AcceptanceCommand`, and `validate_typed_argv()` use typed argv contracts. Generic
shell entrypoints are rejected. Batch 2 does not yet claim authoritative untrusted candidate
execution.

`NetworkPolicy` defaults to `DENY`. Batch 2 introduces no worker network operation.

The deterministic fake worker remains test-only. No production remote, GitHub token, browser
profile, SSH credential, API key, or other secret is passed to it.

Untrusted candidate execution remains intentionally unclaimed. That requires an `OS_SANDBOXED` or
`REMOTE_SANDBOXED` verifier. A worktree alone is not a security boundary. Windows Job Objects,
when integrated, prove process containment only and must be reported as `PROCESS_CONTAINED`, not as
filesystem/network sandboxing.

## Batch 2 job-private workspace substrate

`make_sterile_git_plan()` requires a job root outside the production repository and allocates
private Git metadata at `_nika_private_git`, separate from the worker-visible `worktree`. It never
reuses production `.git` and never places a `.git` directory inside the worker-visible tree.

The sterile Git environment is an allowlist rather than a copy of the host process environment.
Git/GitHub credential variables, SSH agent variables, Python path injection and arbitrary Git
config overrides are omitted. The plan additionally disables system Git config, terminal prompts,
credential helpers, hooks, local file protocol and external protocol helpers. This is a policy
contract for later execution code; it is not yet an execution sandbox.

`collect_tree_evidence()` walks a bounded regular-file tree deterministically, refuses `.git`,
symlinks/reparse points and non-regular entries, enforces file/count/aggregate byte limits, records
per-file SHA-256 and produces an exact ordered tree digest. `assert_production_integrity()` fails
closed if the production base SHA or production tree digest changes across worker execution.

Focused tests include Windows drive/UNC/device/ADS/path traversal cases, Unicode and spaces,
component-scoped allowed roots, symlink escape, credential/config stripping, job-root separation,
shell rejection, literal argv preservation, deterministic/content-sensitive tree evidence, `.git`
refusal, evidence limits and production-integrity mismatch.

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
Nika-side verification logic. The kernel defines/persists this boundary; Batch 2 adds deterministic
tree evidence but still does not execute an untrusted candidate in an independent sandbox.

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

No new runtime dependency is introduced in Batch 1 or this Batch 2 substrate. The implementation
uses Python standard library plus existing Nika contracts and persistence services. Git remains a
thin CLI-adapter target; GitPython/Dulwich are not introduced without a measured requirement.

## What remains unproven / next large Batch 2 slice

The next DEV02 slice must execute the private Git plan through a thin Git CLI adapter, prove remote
removal and branch-collision fail-closed behavior, add typed `shell=False` subprocess execution,
Windows Job Object process-tree containment, cancellation/inspect/recover behavior and physical
Windows reparse/junction tests. After those exact-head gates are green, a thin Codex CLI/SDK
adapter may be added only after current official Windows/API/license verification. Independent
fresh verification workspace, dependency/SBOM/security/license gates and Nika-created verified
candidate identity remain Batch 3 work.
