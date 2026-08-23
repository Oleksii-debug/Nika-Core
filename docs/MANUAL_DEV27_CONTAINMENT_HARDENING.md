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
- `pathlib`, `os`, `stat`, and OS reparse/symlink metadata;
- private Git metadata with no retained remotes;
- sterile Git environment construction;
- deterministic SHA-256 tree evidence.

ADAPT:

- exact executable identity is checked before launch;
- runtime launch requires an absolute pinned executable instead of PATH/CWD lookup;
- every named executable symlink hop is shell-policy checked before dereference;
- `Popen` receives the final canonical resolved executable rather than the allowlisted alias;
- the final canonical executable identity is shell-policy checked again before launch;
- the child environment is filtered again at the process boundary;
- TEMP/TMP/TMPDIR are pinned into the declared worker workspace;
- cwd is required to remain below the declared worker workspace root;
- a cancellation already in force returns before temp setup or process creation, with a second
  cancellation check immediately before `Popen`;
- workspace, evidence and process-temp roots are rejected when the root object itself is a
  symlink or Windows reparse point;
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
3. An allowlisted executable alias cannot hide a forbidden named shell in its symlink chain;
   the runner validates each named hop before dereference and launches only the canonical target.
4. Generic shell entrypoints remain forbidden and arguments remain typed/literal.
5. Child environment input is reduced to the explicit safe environment surface; known tokens,
   arbitrary custom variables, Python path poisoning, SSH agent variables, and Git credential
   overrides are not inherited.
6. Process TEMP/TMP/TMPDIR point into the declared workspace, not a host-supplied temp path.
7. A declared process cwd outside its workspace root fails closed before process launch.
8. A cancellation already set before execution cannot launch the child process or create the
   worker process-temp tree; cancellation is checked again after environment preparation and
   immediately before `Popen`.
9. Guarded workspace, evidence and temp roots fail closed if the root itself is a symlink or
   Windows reparse point; evidence collection must not resolve an attacker-replaced worktree root
   into an external tree before validating the root object.
10. A job root cannot be inside the production repository, equal to it, or contain it.
11. Cleanup does not recursively delete the job root and refuses symlink/reparse content before
    removing the canonical private Git and worktree roots.
12. Output delta evidence detects additions, modifications, and deletions deterministically,
    enforces allowed path scope, and enforces the changed-file budget.
13. `.github/workflows` and `.github/actions` mutations are denied by default by the output
    provenance boundary unless a trusted higher-level control-plane approval explicitly opts in.

## AUD02 executable-indirection repair

Independent AUD02 QA-only PR #199 reproduced a real command-boundary defect against an earlier
DEV27 candidate. An exactly allowlisted absolute alias could point to `/bin/sh`; the old runner
validated only the alias name, resolved it, and then launched the resolved target without
preserving the shell-policy evidence from the resolution chain.

The QA-only oracle failed on Ubuntu exactly as intended: the forbidden shell executed and the
test reported that no security exception was raised. Windows skipped that portable `/bin/sh`
fixture, so the finding is specifically independent POSIX evidence rather than a claim about the
separate PR #72 physical Windows proof.

The production repair keeps safe symlinked executables usable while closing that attack family:

1. the originally requested executable still has to match the trusted `ProcessPolicy` allowlist;
2. the executable must still be an absolute path;
3. every named symlink hop is visited with loop/depth bounds and reuses the same generic-shell
   validation before dereference;
4. the final canonical target is resolved strictly and generic-shell validation is applied again;
5. only that canonical path is passed to `subprocess.Popen(..., shell=False)`;
6. a symlink loop or invalid/missing target fails before process launch.

DEV27 regressions cover a nested alias chain through `/bin/sh`, a legitimate safe Python symlink
that must launch the canonical Python target, and a symlink loop that must fail before launch.
The independent AUD02 attack must still be replayed on the final exact DEV27 head; DEV27 does not
self-clear the `AUD02-BLOCK` label from its own regression evidence.

This repair is command-boundary hardening, not an immutable executable-content attestation
system. It does not claim protection against replacement of an otherwise approved executable file
between policy creation and launch, nor against a general-purpose interpreter deliberately granted
by policy being used as arbitrary code. Those stronger hostile-code guarantees require trusted
artifact identity and/or real OS/remote isolation rather than path-name validation alone.

## Root-level indirection repair

A second DEV27 self-audit found that the earlier `collect_tree_evidence()` implementation resolved
its root before rejecting symlink/reparse entries below it. If a worker replaced the entire
worktree root with an indirection to another tree before evidence capture, provenance could be
collected from the wrong filesystem authority.

The shared low-level root guard now performs `lstat()` on the supplied root before canonical
resolution, rejects symbolic links and Windows reparse points, requires an actual directory, and
is reused by guarded workspace paths, process temp roots and tree-evidence collection. Focused
regressions prove that a symlinked evidence root, workspace root and process-temp root all fail
closed. Physical Windows junction evidence owned by PR #72 remains separate and is not copied into
this lane.

## Pre-launch cancellation repair

A third DEV27 self-audit found a deterministic side-effect race in the old process runner. When a
`cancellation_event` was already set before `run_typed_process()` was called, the runner still
created the child with `Popen` and only noticed the cancellation in its post-launch polling loop.
A short-lived command could therefore perform an immediate filesystem or external side effect even
though cancellation authority already existed before dispatch.

The runner now checks cancellation after trusted argv/cwd/workspace validation but before process
environment/temp preparation, and checks it a second time immediately after environment setup and
before `Popen`. A pre-cancelled request returns a typed failed/cancelled `ProcessExecutionResult`
without creating the worker temp directory or launching the child. The focused regression uses an
immediate marker-writing Python command and proves both the marker and `_nika_process_tmp` remain
absent.

This does not claim a mathematically atomic cancellation-to-process-creation transaction. A new
cancellation can still race in the very small interval after the final check and before the OS
creates the process; once a process exists, existing Job Object/process-group termination remains
the containment mechanism. Eliminating that residual launch race requires a stronger OS-specific
suspended-process/dispatch primitive or a remote sandbox transaction, not an inaccurate Python
object-level claim.

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
- The AUD02 executable-indirection attack family must be independently replayed on that exact
  candidate and clear the blocker; prior-head QA evidence cannot clear a newer candidate.
- Compatibility with PR #72 is architectural unless/ until its physical Windows test is present
  in the same integrated main; this lane does not copy another owner's test into its PR.
- `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` unless a real human/NVDA run is recorded.