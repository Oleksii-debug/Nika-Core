# MANUAL-DEV07 PF5 Build Execution Integrity / Authority Addendum — 2026-08-23

This addendum records the current repair lineage for PR #177.

## Superseded exact head

`ff30f63abbf03b061680b7bdce331c245d559235` is RED and receives no acceptance credit.

Exact source-gate evidence:

- Core CI #1100 / run `32643958861`: failure on both Windows and Ubuntu at Ruff;
- M12 #868 / run `32643958865`: failure in the same source-gate family;
- PF3 Windows credential proof #332: skipped as expected/out of scope;
- dependency consistency and exact candidate checkout passed before Ruff;
- Ruff defects were exactly `BLE001` on broad node-port exception handling and `PIE810` on redundant
  string-prefix checks.

The gate is not weakened or ignored.

## AUD02 BLOCK addressed by this repair

AUD02 correctly reported that the old candidate was candidate-controlled authority:

1. caller-owned `BuildExecutionSpec` embedded node/path/network/credential authority;
2. caller-owned `argv` could select arbitrary executable/shell behavior.

The repaired contract removes both fields from candidate control.

Candidate state now contains only a bounded `BuildExecutionScopeRequest`. A trusted composition-root
`TrustedExecutionAuthorityPort` independently resolves exact `(project, repository, work)` authority,
including permission provenance, node allowlist, workspace roots, network scopes, credential refs,
approved build commands, and evidence refs.

`submit()` requires `build_release` within that trusted permission authority and accepts only subset
requests. The durable `ExecutionGrant` is derived from the host result, not supplied by the candidate.
The authority is re-resolved before effect and on restart.

Host-approved command IDs resolve to exact argv. Generic shell executables are rejected at this PF5
boundary; DEV27 still owns low-level process/workspace containment.

## Restart / uncertain-effect hardening retained

The earlier integrity family remains active:

- exact lease ID/node binding;
- node capability/resource/platform revalidation;
- deterministic dispatch ID;
- strict integer/boolean persisted identities;
- corrupted grant/evidence rejection;
- restart after possible external effect is inspection-only.

The repaired state machine adds `EFFECT_IN_FLIGHT` before node-port invocation. Expected transport
failure is typed as `BuildExecutionPortError`. Unexpected programming exceptions propagate while state
remains in-flight, preventing a blind second `run()`.

Authority revocation is safe by phase:

- before external effect: release capacity and `WAITING_FOR_AUTHORITY`, no port call;
- after a possible effect: preserve dispatch identity and permit inspection/reconciliation only.

## Current deterministic preflight

Against the integrated PF3 public contract shapes, repaired production/tests currently pass:

- `python -m py_compile`: PASS;
- focused deterministic pytest: **52 passed**;
- line-length <=100: PASS.

The 52 instances include the prior restart-corruption matrix plus new authority substitution,
permission escalation, shell-command, TOCTOU revocation, and unexpected-exception replay attacks.

Local Ruff is unavailable; fresh exact-head GitHub Core CI + complete M12 are mandatory. Earlier GREEN
or queued heads do not transfer.

## Evidence boundary

No real provider, cloud, SSH, WinRM, remote host, macOS/Xcode, GPU, production, credential secret, or
release action is executed by these tests. Snapshot/restart semantics are tested deterministically; a
concrete production persistence host remains a separate integration proof and is not falsely claimed.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`NO_SELF_MERGE=true`
