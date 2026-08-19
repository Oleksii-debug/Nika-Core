# M10 atomic authorization ledger

Updated: 2026-08-19.

Status: stacked implementation candidate. Depends on PR #60 target-scope hardening. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Problem

M10 keeps two mutable authorization resources: a one-time `ApprovalLedger` for exact human evidence and an `ExecutionBudgetLedger` for write/network/process ceilings.

Previously `authorize_action()` consumed approval first and reserved budget second. If the action had valid approval but no remaining budget, authorization failed after the one-time approval had already been spent. Reversing the two calls would merely invert the bug: a missing or invalid approval could consume budget even though no action was authorized.

The correct downstream invariant is that approval evidence and budget reservation commit together or neither commits.

## REUSE / ADAPT / CUSTOM

REUSE:

- Python standard-library `threading.RLock`, including its context-manager protocol;
- `dataclasses.field(default_factory=..., init=False, repr=False, compare=False)` for per-ledger synchronization state that does not become part of the public constructor, representation or equality contract;
- existing M10 `ActionIntent`, exact approval fingerprint, security policy and resource-budget models.

ADAPT:

- existing ledger operations are split internally into non-mutating validation and non-failing commit phases while their public `consume()` / `reserve()` behavior remains compatible;
- `authorize_action()` acquires approval then budget locks in one fixed order, validates both resources without mutation, and only then commits both.

CUSTOM thin:

- one in-process atomic authorization critical section spanning the two existing ledgers.

No dependency, database or cross-process transaction framework is introduced.

## Invariants

For a policy-valid action:

1. static tool/path/network/executable checks run before mutable authorization state;
2. approval and budget locks are acquired in a single fixed order: approval first, budget second;
3. next budget usage is checked without mutation;
4. required approval is checked for presence, exact fingerprint, validity window and prior use without mutation;
5. only after both validations succeed are the budget counters and approval-used marker committed;
6. commit helpers used by the combined path do not contain a second fallible validation step.

Consequences:

- budget rejection does not consume valid human evidence;
- missing, mismatched, expired or reused approval does not reserve budget;
- competing threads sharing the same ledgers cannot interleave the two-resource commit;
- a budget loser keeps its exact one-time approval and can be retried if a legitimate fresh budget becomes available;
- successful authorization still consumes approval exactly once and reserves budget exactly once.

## Scope boundary

This provides **in-process thread safety only** for the in-memory M10 ledgers. It is not a durable transaction, a distributed lock or crash-recovery journal. A future durable authorization store must provide its own transactional semantics rather than assuming `RLock` survives process termination.

The critical section protects only authorization accounting. It does not claim that the external side effect itself is atomic with the ledger commit. Adapters must still execute only after `authorize_action()` succeeds and must use their own idempotency/recovery strategy where an external operation can fail after authorization.

## Acceptance evidence

The focused regressions must prove:

1. budget rejection leaves one-time approval unconsumed;
2. missing approval leaves all budget counters unchanged;
3. mismatched approval leaves budget unchanged;
4. two competing approved high-impact writes against one remaining byte produce exactly one success and one budget rejection;
5. the losing concurrent action can still use its untouched approval with a legitimate fresh budget;
6. a success commits budget and consumes approval once;
7. existing direct `ApprovalLedger.consume()` and `ExecutionBudgetLedger.reserve()` semantics remain compatible;
8. the complete repository suite remains green on Ubuntu and Windows after PR #60 is integrated.

No integration or packaging credit is claimed for this stacked branch before its parent security slice is integrated and its own exact-head gates run.
