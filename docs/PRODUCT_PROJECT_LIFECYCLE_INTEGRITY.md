# ProductProject Core and Lifecycle Integrity

Status: MANUAL-DEV01 PF0/PF12 durability hardening.

## Scope and ownership boundary

This contract hardens the canonical `product_project.py` durable read/write boundary together with `product_project_lifecycle.py`. Portable-history files remain outside this lane because the active PF1 strict-numeric owner is responsible for that separate family. This change does not depend on the alternate spec-mutation path in PR #166.

## Canonical durable version identity

`ProductProjectRepository` is the authority that first reads core ProductProject rows. It must therefore reject corrupted numeric identity before constructing a domain object: `row_version` is an exact non-negative integer and `current_spec_version` is an exact positive integer. Specification-history version rows are validated on read as well.

`update_spec(expected_row_version=...)` accepts only an exact non-negative Python integer. Boolean, floating-point, string and negative aliases are rejected before database mutation. `ProductProjectSpec.supersedes_spec_version`, when present, is an exact positive integer.

This canonical validation is intentionally upstream of lifecycle reconstruction so a raw SQLite REAL such as `0.5` cannot be normalized with `int(...)` before integrity checks observe it.

## Lifecycle durable version identity

Lifecycle optimistic concurrency also accepts only exact non-negative Python integers for `expected_row_version`. Durable project and lifecycle-idempotency versions are treated as exact integer identity rather than normalized.

## Restart and corrupted-state behavior

Persisted status audit evidence is parsed fail-closed. A durable event must contain exact string states, an exact positive integer row version, non-empty string reason and actor reference, and a timezone-aware ISO-8601 timestamp. Token-shaped credential material remains forbidden on both write and read paths.

History reconstruction proves:
- status audit row versions increase monotonically even when specification mutations consume row versions between lifecycle events;
- no status event claims a row version newer than the durable ProductProject;
- every event's `previous_state` matches the prior durable lifecycle state;
- the final reconstructed lifecycle state matches the ProductProject's durable status.

A non-active durable status without matching lifecycle evidence therefore fails closed after restart instead of being silently represented as a fabricated baseline.

## Idempotent replay integrity

The existing idempotency input fingerprint is retained for compatibility. On replay, the stored entity version must be exact and the matching durable audit event is re-fingerprinted from project id, requested state, reason and actor. Tampered audit evidence can no longer be returned as a successful replay merely because its row version still matches.

## Evidence truth

Focused tests cover raw REAL core-row corruption at both repository and lifecycle restart boundaries, expected-version coercion attacks, boolean lineage versions, boolean/rehashed audit versions, malformed actor types, idempotent replay tamper, state-chain tamper, non-monotonic versions, missing audit evidence and credential-shaped read tamper. A valid integer spec revision remains the positive compatibility control.

Automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
