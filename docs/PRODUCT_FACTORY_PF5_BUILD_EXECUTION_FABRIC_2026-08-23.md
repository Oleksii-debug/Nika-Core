# PF5 Build Execution Fabric — 2026-08-23

## Current lane

- One-shot lane: `ONE-SHOT-08 / PF5 build execution fabric`.
- Starting/current-main boundary for this successor: `3fbfabfc93d59183f174ff44098db886cff93bd8`.
- Branch: `work/one-shot-08/pf5-durable-build-execution`.
- Predecessor: PR #177 / `f44fa9a64c5641274770e226d61816f8a9da4a9c`.
- The predecessor's verified PF5 state-machine files were carried unchanged onto current main before the
  durable-host extension.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- No real provider deployment, SSH, WinRM, cloud, release promotion, or credential-secret use occurs.

## Acceptance target

PF5 must fail closed unless all execution identity and authority comes from trusted host state. A
candidate may request bounded scope, but it may not carry execution authority, raw shell authority, or
credential material. The trusted host resolves the exact project/repository/work command, execution
node, workspace, network and credential ceiling.

The useful slice also requires a durable external-effect boundary: `EFFECT_IN_FLIGHT` must be committed
to canonical storage before a real node port is invoked. Lost acknowledgement or restart after that
boundary is inspection/reconciliation-only and must never replay the build blindly.

## REUSE → ADAPT → CUSTOM(thin)

### REUSE

PF5 reuses current integrated contracts instead of creating a second execution framework:

- `ExecutionNodeRegistry`, `ExecutionRequest`, `WorkLease`, `ExecutionRegistrySnapshot`;
- `Platform`, node capability/resource/GPU matching and `NormalizedBuildEvidence`;
- canonical `SQLiteStore`, `tasks`, `checkpoints` and `audit_events`;
- canonical Product Factory host task identity
  `{"kind":"product_factory","product_project_id":...}`;
- Toolsmith `AllowedPathPolicy`, `ChangedFile`, and repository-relative path normalization;
- Product Factory's trusted-host path-identity concept for Windows/Linux changed-file evidence.

No new dependency, provider SDK, generic scheduler, shell framework, credential store, or persistence
framework is introduced.

### ADAPT

Existing contracts are adapted with:

- host-resolved `ProjectExecutionAuthority` and candidate-scope subset checks;
- host-approved typed command IDs resolving to exact argv;
- generic-shell rejection at the approved-command boundary;
- `EFFECT_IN_FLIGHT` / `RECONCILE_REQUIRED` state semantics;
- `SQLiteBuildExecutionCheckpointStore` over the existing canonical `checkpoints` table;
- `DurableBuildExecutionHost` as the production side-effect composition;
- trusted `BuildOutputPolicy` for changed-file count/path/case semantics;
- exact restart reconstruction of PF5-owned leases against the current trusted node registry.

### CUSTOM(thin)

PF5-specific code remains limited to the build request/grant/dispatch/result state machine, its durable
checkpoint codec/store, the host effect decorator, and build-output evidence policy. DEV27 remains the
owner of low-level process, workspace, reparse-point, OS isolation and containment. PF5 does not
replicate those mechanisms.

## Candidate authority and shell safety

`BuildExecutionSpec` contains `ExecutionRequest`, source SHA, a bounded `BuildExecutionScopeRequest`, and
lease duration. It contains neither `ProjectExecutionAuthority` nor arbitrary `argv`.

`TrustedExecutionAuthorityPort` independently resolves authority for the exact
`(project_id, repository_id, work_id)`. The resolver must return matching identity, `build_release`
permission, allowed node IDs, project-relative workspace roots, network scopes, opaque `credref:`
identities, host-approved commands and authority evidence refs. Every candidate request must be a subset
of this authority.

Approved command entrypoints reject generic shells including cmd, PowerShell/pwsh, sh/bash/zsh/fish
and WSL, including path-qualified forms. This is a PF5 authority guard only; DEV27 still owns process
containment.

## Durable external-effect boundary

`BuildExecutionCoordinator` remains provider-neutral. `DurableBuildExecutionHost` is the effectful
production composition and checkpoints every state-changing operation through
`SQLiteBuildExecutionCheckpointStore`.

For a real run:

1. the coordinator re-resolves current execution authority;
2. it changes the record to `EFFECT_IN_FLIGHT`;
3. the host's node-port decorator verifies exact dispatch/state and writes that state to SQLite;
4. only after the checkpoint succeeds may the real `BuildExecutionNodePort.run()` execute;
5. definite output evidence is validated against current trusted output policy;
6. terminal or reconciliation-required state is checkpointed again.

If persistence fails, the process-local durable host is poisoned and refuses further execution until a
fresh host restores canonical durable state. If the node port loses acknowledgement, PF5 records
`RECONCILE_REQUIRED`. Repeated `execute()` does not invoke `run()` again; only `inspect()` may reconcile
the exact dispatch.

Unexpected adapter/provider programming exceptions are not converted to fake success. SQLite remains at
`EFFECT_IN_FLIGHT`, so restart becomes inspection-only.

## Canonical SQLite persistence

`SQLiteBuildExecutionCheckpointStore` uses the existing `checkpoints` table with stage
`product_factory.build_execution.v1`; it does not add a new database or migration. The host task must
already exist in canonical `tasks` and must be bound to the exact ProductProject identity.

Each checkpoint has:

- monotonic exact-integer transition sequence;
- canonical JSON payload with a fixed PF5 schema marker;
- SHA-256 checksum;
- deterministic checkpoint ID;
- exact coordinator records;
- PF5-owned active leases and registry next-lease counter;
- exact changed-file evidence;
- a matching `product_factory.build_execution_checkpoint_saved` audit event in the same SQLite
  transaction.

The decoder is allowlist-only for known PF5 dataclasses/enums/tuples/frozensets/datetimes. It does not
use pickle, dynamic imports, or candidate-selected types. Malformed/checksum-invalid/project-substituted
state fails closed.

## Restart semantics

A new host that detects durable PF5 state refuses execution until `restore_latest()` is called.

Restore reconstructs only PF5-owned persisted leases into the **current** `ExecutionNodeRegistry`; it
does not restore stale node definitions from the checkpoint. Current node identity, platform,
capabilities, resources, availability and trusted execution authority are revalidated by the existing
coordinator restore path.

A still-valid PREPARED lease can resume. An expired or unavailable PREPARED lease is normalized to
`WAITING_FOR_NODE`. Restart across `DISPATCHING` or `EFFECT_IN_FLIGHT` releases capacity and becomes
`RECONCILE_REQUIRED` while retaining exact dispatch identity. Lease/node/work collisions with current
registry state fail closed.

## Windows/Linux normalized evidence and output budget

Definite node results still become the existing `NormalizedBuildEvidence`; Windows and Linux therefore
share the same release/artifact evidence contract.

PF5 additionally asks a read-only `BuildExecutionFileEvidencePort` for changed files and validates them
against a current host-owned `BuildOutputPolicy`:

- exact project/repository/work identity;
- explicit allowed repository-relative paths;
- exact non-negative `max_changed_files`;
- Windows case-insensitive path identity;
- Linux case-sensitive path identity;
- duplicate/case-alias rejection as appropriate;
- normalized relative paths with no absolute, drive, `..`, or `.git` escape.

Terminal durable work must carry exact file evidence bound to its dispatch/source/platform. Non-terminal
work cannot claim terminal file evidence. Restart revalidates that evidence against current trusted
output policy.

## Platform-unavailable semantics

If no currently authorized capable node is available, PF5 becomes `WAITING_FOR_NODE`; it does not call
the node port and does not fabricate success evidence. The reason names the requested platform and GPU
requirement where relevant. Existing node matching remains authoritative for enabled state, platform,
features, toolchains, GPU and resource capacity.

## Explicit non-claims

This slice does not prove physical remote Windows/Linux/macOS execution, a real GPU/on-prem provider,
real credential resolution, SSH/WinRM/cloud APIs, staging/production promotion, or human/NVDA behavior.
Those require their separate physical/provider acceptance evidence.
