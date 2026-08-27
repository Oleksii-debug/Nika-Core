# ProductProject Lifecycle Integrity

Status: MANUAL-DEV01 PF0/PF12 durability hardening.

## Scope and ownership boundary

This contract hardens `product_project_lifecycle.py` only. Portable-history files remain outside this lane because the active PF1 strict-numeric owner is responsible for that family.

## Durable version identity

Lifecycle optimistic concurrency accepts only exact non-negative Python integers for `expected_row_version`. Boolean, floating-point, string and negative coercions are rejected before any database mutation. Durable project and idempotency row versions are also treated as exact integer identity rather than normalized with `int(...)`.

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

Focused tests cover coercion attacks, boolean rehashed audit versions, malformed actor types, idempotent replay tamper, state-chain tamper, non-monotonic versions, missing audit evidence and credential-shaped read tamper.

Automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
