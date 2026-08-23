# MANUAL-DEV27 Coding Worker Containment Hardening

Status: implementation candidate in `work/manual-dev27/containment-hardening`.

Starting canonical main for the lane was `bd7517f38c04560aa7350b870d8a51bfb6c8113b`.
The lane was then merged forward, without force push, to include main
`e40691a6e2ff9c31fd413f63d004612e048d95ed` after PF2 advanced during the run.

## Ownership boundary

This lane owns low-level coding-worker workspace/process containment primitives,
filesystem boundary checks, cleanup, and output provenance.

It does not own Product Factory worker adaptation. High-level Product Factory mapping
continues to consume the framework-neutral `CodingWorkerPort` contract through the DEV06
boundary.

PR #72 remains the owner of physical Windows junction/reparse and Job Object descendant
termination proof. This lane intentionally does not edit
`tests/test_toolsmith_windows_security.py` or change the Windows Job Object semantics
covered there.

## REUSE -> ADAPT -> CUSTOM (thin)

REUSE:

- `subprocess` typed argv with `shell=False`;
- Windows Job Objects already used by the process runner;
- `pathlib`, `stat`, and OS reparse/symlink metadata;
- private Git metadata with no retained remotes;
- sterile Git environment construction;
- deterministic SHA-256 tree evidence.

ADAPT:

- exact executable identity is checked before launch;
- runtime launch requires an absolute pinned executable instead of PATH/CWD lookup;
- the child environment is filtered again at the process boundary;
- TEMP/TMP/TMPDIR are pinned into the declared worker workspace;
- cwd is required to remain below the declared worker workspace root;
- production repository and job workspace roots must be fully disjoint in both directions;
- cleanup removes only canonical private Git/worktree roots and refuses reparse/symlink
  content;
- before/after tree evidence is converted into deterministic added/modified/deleted delta
  evidence and checked against allowed paths and changed-file budget.

CUSTOM:

Only Nika-specific policy glue: control-plane path classification, evidence records, and
fail-closed validation. No alternate generic sandbox framework is introduced.

## Security invariants covered by this batch

1. A path-qualified executable cannot pass an allowlist merely because its basename matches.
2. A worker process cannot rely on PATH/CWD executable search in `run_typed_process`.
3. Generic shell entrypoints remain forbidden and arguments remain typed/literal.
4. Child environment input is reduced to the explicit safe environment surface; known tokens,
   arbitrary custom variables, Python path poisoning, SSH agent variables, and Git credential
   overrides are not inherited.
5. Process TEMP/TMP/TMPDIR point into the declared workspace, not a host-supplied temp path.
6. A declared process cwd outside its workspace root fails closed before process launch.
7. A job root cannot be inside the production repository, equal to it, or contain it.
8. Cleanup does not recursively delete the job root and refuses symlink/reparse content before
   removing the canonical private Git and worktree roots.
9. Output delta evidence detects additions, modifications, and deletions deterministically,
   enforces allowed path scope, and enforces the changed-file budget.
10. `.github/workflows` and `.github/actions` mutations are denied by default by the output
    provenance boundary unless a trusted higher-level control-plane approval explicitly opts in.

## Isolation truth and non-goals

`POLICY_ONLY` and Windows `PROCESS_CONTAINED` remain exactly what their names state.
They are not filesystem or network sandboxes. This batch does not relabel them and does not
claim that Python validation can confine arbitrary hostile code.

The documented Popen-to-Job assignment race remains a limitation of the current Windows
process-contained runner and is not silently reclassified as solved.

Network-deny or approved-host enforcement for untrusted coding execution still requires a
real OS or remote sandbox adapter. A future OpenHands, Codex, container, VM, Windows Sandbox,
or equivalent worker must remain behind `CodingWorkerPort` and must provide independently
verified isolation evidence before it can be classified `OS_SANDBOXED` or
`REMOTE_SANDBOXED`.

A worker process that can execute arbitrary Python or another general-purpose interpreter
must be treated as arbitrary code. Executable allowlisting is command-boundary hardening,
not a substitute for OS isolation.

## Required integration sequence for a real coding-worker adapter

1. Create a dedicated job root fully disjoint from production source and metadata.
2. Create private Git metadata/worktree with `make_sterile_git_plan` and
   `prepare_private_git_workspace`.
3. Capture the initial `TreeEvidence`.
4. Launch only through a real isolation adapter appropriate to the job's network/filesystem
   policy. If the low-level typed process runner is used for trusted bounded commands, supply
   a pinned absolute executable and the declared workspace root.
5. Capture final `TreeEvidence` and derive `TreeDeltaEvidence` with the job allowed paths and
   changed-file budget.
6. Independently verify candidate tests and exact digests; do not trust worker self-report.
7. Re-check production repository integrity.
8. Cancel/terminate process descendants before cleanup.
9. Clean only the private Git/worktree roots with `cleanup_private_git_workspace`.
10. Promotion/registration remains outside the worker and cannot expand the original task
    permission ceiling.

## Acceptance evidence required before integration credit

- Ruff, compile, dependency consistency and full pytest must be green on the exact candidate
  SHA through Core CI on Ubuntu and Windows.
- M12 pre-human release gate must be green on that same exact SHA where applicable.
- Compatibility with PR #72 is architectural unless/ until its physical Windows test is present
  in the same integrated main; this lane does not copy another owner's test into its PR.
- `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` unless a real human/NVDA run is recorded.
