# MANUAL-DEV07 PF5 Build Execution Integrity Addendum — 2026-08-23

This addendum is part of the same MANUAL-DEV07 PF5 build-execution-fabric batch and supersedes the
initial focused-test count in `docs/PRODUCT_FACTORY_PF5_BUILD_EXECUTION_FABRIC_2026-08-23.md`.

## Why this hardening was added

Exact-source review after the initial candidate found a restart corruption family: a coordinator
`PREPARED` or `DISPATCHING` record could meet a registry lease for the same project/work identity but
with a substituted lease ID or node. Treating that as ordinary capacity recovery can hide durable
lineage corruption and can leave a substituted lease active. PF5 requires corrupted state to fail
closed rather than normalize it into a plausible execution state.

## Current hardening

The coordinator now additionally requires on restart:

- exact active `lease_id` and `node_id` agreement between coordinator record and registry;
- selected node must remain inside the project's explicit authorized-node set;
- the registry node behind an active lease must still satisfy platform, enabled state, resources,
  required features, toolchains and GPU requirement;
- post-dispatch `dispatch_id` must equal the deterministic project/work/attempt identity rather than
  merely being a non-empty candidate-controlled string;
- persisted execution attempts and lease duration reject Python bool aliases and non-integer values;
- execution result success/uncertainty fields must be exact booleans.

Missing or expired exact PREPARED lease remains a recoverable capacity event: the work returns to
`WAITING_FOR_NODE`. A same-project/work lease with conflicting identity is different: it is ambiguous
or corrupted authority and restore raises `BuildExecutionError`.

The implementation still does not edit the active PF3 fleet-replacement production slice or the
MANUAL-DEV09 promotion slice.

## Focused qualification delta

The original focused suite has 23 pytest parameter instances. The integrity addendum adds 12 more,
for a current focused total of **35**:

- same work with substituted lease identity;
- same lease identity rebound to another node;
- authorized node capability drift across restart;
- restored node outside project authority;
- forged dispatch identity;
- bool/float/string lease-duration coercion;
- integer/string status values masquerading as booleans;
- bool dispatch-attempt alias.

All new tests are deterministic and provider-free. No cloud, SSH, WinRM, macOS/Xcode, GPU, on-prem,
staging, production or real credential action is performed.

## Acceptance truth

Only GitHub Actions on the final PR head after this addendum may receive exact-head acceptance credit.
Earlier queued/superseded heads are lineage evidence only. Core CI and complete M12 must both be
terminal GREEN on that same final SHA. PF3 credential-store proof may be skipped when the path filter
correctly classifies this branch out of scope.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`NO_SELF_MERGE=true`

Integration remains TECH02-owned.
