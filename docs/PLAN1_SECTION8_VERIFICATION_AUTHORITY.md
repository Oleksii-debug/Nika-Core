# Plan 1 Section 8: exact-SHA verification authority convergence

Status: IMPLEMENTED candidate, not integrated or Section 8 DONE.

## Confirmed failure and canonical reuse

Source baseline: main b24c36f0781ceea743a48f86a4d1beacbcb1a656.
The incumbent Product Factory classifier accepted a previously constructed
ExactShaCheckEvidence after its state was changed from UNKNOWN to the plain
string `pass`. That non-enum value matched none of the identity-based negative
states and fell through to classifier-owned PASS with merge_clearance=True.
The actual baseline reproduction used the real classifier and canonical
`core`/`factory` required profile, without a user database or external effect.

REUSE the same existing classifier and its bounded exact-head evidence contract:

| Canonical lineage | Reused behavior |
| --- | --- |
| #879, 59e85c8a024a3b29a4b61b3a8da0c18f7e544e3e | Exact evidence envelopes, detached validated snapshots, enum/text/bool carriers and required-profile admission before classification |
| #1007, 97d0ef612b1412a51fb0281a41363e9e9b724e31 | Clearance bound to unchanged classifier-issued result identity |
| #1010/#1011 | Bounded evidence count/check IDs and result integrity regressions |
| #1012, b1d0b5a3eb559b36b11c66f61b7113ac76bc00aa | Printable UTF-8 evidence identities |
| #1013, 97a47f3417efde40fa4928f1b1c75de0f36d9e86 | Structural validity before STALE/MISMATCH/incomplete state classification |

The six original test files are preserved byte-for-byte from final donor #1013.
Its production blob 2883488046099bc5e8488b647979a39a11de6b5a is the reused
source baseline. Independent review found a further same-authority enum
allocation bypass, requiring the narrow compatibility repair below. No new
verifier, CI adapter, persistence authority, workflow or dependency.

## Qualification evidence and limits

The six donor modules against unchanged production executed 92 cases:
51 failed, 41 passed. After source convergence: 92 passed; owned Ruff and
`git diff --check` passed. Coverage includes altered evidence/result carriers,
behavioral values rejected before their behavior, exact-SHA stale/mismatch,
missing authoritative gates, duplicate or unauthorized required checks,
invalid UTF-8/control text, input bounds, canonical PASS/FAIL/RUNNING/UNKNOWN,
and unchanged optional-check behavior.

This pure classifier has no database or recovery store. Re-admission and repeated
classification use the same canonical observations; a prior PASS cannot transfer
to another SHA. This is not a new physical crash/restart test or proof that an
untrusted observation came from an independent CI producer.

The result-identity guard detects partial/accidental low-level mutation. Arbitrary
hostile in-process Python could also forge private fields or replace code. Never
treat a caller-supplied/deserialized result carrier as independent authorization:
read trusted exact-repository/current-head required CI and review evidence.

Independent review reproduced `str.__new__(CheckState, raw)`: the allocated
object has the exact enum class but is not one of its canonical members. It
bypassed the donor's type gate and could fall through to PASS. A similarly
allocated VerificationState seeded a string-equal result-identity seal and
allowed a subsequent one-field change to mint clearance.

The compatibility repair requires identity with a canonical member for both
state enums. The positive classification path also requires every authoritative
required item to be the canonical CheckState.PASS singleton. It never uses
string equality or set membership to establish enum authority.

The additional module executed 22 cases against the donor convergence:
18 failed, 4 passed. After repair, all 114 cases across the seven focused modules
passed, including explicit string-to-canonical-member conversion and the
original six donor suites. The older 4253-pass full run qualifies only the
pre-enum-review source and is historical. New full verification, independent
review, exact-head hosted Ubuntu/Windows/package gates and scoped integration/
readback remain required.
The ancestor PRs must not be independently ancestry-merged. Qualify one scoped
canonical finisher and preserve the actual accepted main elsewhere.

This closes one confirmed Section 8.3 source gap only. Actor/action/input/result/
effect causal chains, typed producer/source/build/release provenance, retention,
trusted external chronology, and the durable user-facing audit/artifact journey
retain their own acceptance work. Artifact byte verification is not semantic
PASS, release readiness, or credential/effect authority.

HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false.
No credentials, production data, private logs, browser profiles or model weights.
