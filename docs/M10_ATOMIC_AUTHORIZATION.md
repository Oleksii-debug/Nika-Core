# M10 atomic authorization and exact approval identity

Updated: 2026-08-19.

Status: implementation candidate on top of integrated PR #60. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Problems

M10 keeps two mutable authorization resources: a one-time `ApprovalLedger` for exact human evidence and an `ExecutionBudgetLedger` for write/network/process ceilings.

Previously `authorize_action()` consumed approval first and reserved budget second. If a valid high-impact action had no remaining budget, authorization failed after the one-time approval had already been spent. Reversing the two calls would merely invert the bug: a missing or invalid approval could consume budget even though no action was authorized.

A second audit finding affected the meaning of "exact" approval itself. `ActionIntent.approval_fingerprint` previously concatenated fields with the control character `U+001F` and replaced optional `None` values with empty strings. Two different field tuples could therefore serialize to the same pre-hash payload: a separator embedded inside one field could shift the apparent field boundary, and `None` could alias `""`. The `approval_required` flag was also absent from the fingerprint.

The downstream invariants are therefore:

1. approval evidence and budget reservation commit together or neither commits;
2. every semantically distinct `ActionIntent` field tuple has an unambiguous canonical representation before SHA-256 hashing.

## REUSE / ADAPT / CUSTOM

REUSE:

- Python standard-library `threading.RLock`, including its context-manager protocol;
- `dataclasses.field(default_factory=..., init=False, repr=False, compare=False)` for per-ledger synchronization state that does not become part of the public constructor, representation or equality contract;
- Python standard-library `json.dumps()` for framed structured serialization of the fingerprint tuple;
- existing SHA-256 digest, `ActionIntent`, security policy and resource-budget models.

ADAPT:

- existing ledger operations are split internally into non-mutating validation and non-failing commit phases while their public `consume()` / `reserve()` behavior remains compatible;
- `authorize_action()` acquires approval then budget locks in one fixed order, validates both resources without mutation, and only then commits both;
- the fingerprint hashes a versioned JSON array using compact separators rather than an unescaped delimiter string. Optional values stay JSON `null` rather than collapsing to empty strings, numeric and Boolean fields keep their types, and `approval_required` is included.

CUSTOM thin:

- one in-process atomic authorization critical section spanning the two existing ledgers;
- the versioned `nika-action-intent-v1` field schema that defines Nika's exact approval identity.

No dependency, database or cross-process transaction framework is introduced.

## Atomic ledger invariants

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

## Exact fingerprint invariants

The SHA-256 input is a JSON array containing, in a fixed order:

1. schema tag `nika-action-intent-v1`;
2. `action_id`;
3. `tool_id`;
4. risk value;
5. target;
6. `write_path` including `null` when absent;
7. numeric `write_bytes`;
8. `network_host` including `null` when absent;
9. `executable` including `null` when absent;
10. Boolean `approval_required`.

JSON string escaping frames embedded control characters instead of treating them as field boundaries. The schema tag makes future incompatible fingerprint layouts explicit rather than silently reinterpreting old evidence.

Changing this fingerprint intentionally invalidates approval evidence produced against the ambiguous legacy serialization. That is a fail-closed security migration: human approval must match the current exact action contract.

## Scope boundary

The ledger change provides **in-process thread safety only** for the in-memory M10 ledgers. It is not a durable transaction, distributed lock or crash-recovery journal. A future durable authorization store must provide its own transactional semantics rather than assuming `RLock` survives process termination.

The critical section protects authorization accounting only. It does not make the external side effect atomic with the ledger commit. Adapters must execute only after `authorize_action()` succeeds and still need idempotency/recovery appropriate to their external operation.

The canonical fingerprint prevents structural serialization aliasing; it does not turn SHA-256 into an authentication mechanism. Authenticity and provenance of `ApprovalEvidence` remain separate concerns for the human-approval channel.

## Acceptance evidence

Focused regressions must prove:

1. budget rejection leaves one-time approval unconsumed;
2. missing or mismatched approval leaves budget unchanged;
3. two competing approved high-impact writes against one remaining byte produce exactly one success and one budget rejection;
4. the budget loser retains its exact approval for a legitimate fresh budget;
5. success commits budget and consumes approval once;
6. the historical embedded-separator payload alias no longer produces equal fingerprints;
7. absent optional values and explicit empty strings no longer alias;
8. `approval_required` participates in exact identity;
9. evidence generated for one formerly colliding action is rejected for the other;
10. the complete repository suite remains green on Ubuntu and Windows with exact-head M12 pre-human packaging evidence before integration.
