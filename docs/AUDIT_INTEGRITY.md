# Generic Audit Integrity

## Scope

`nika_core.kernel.audit.AuditLog` is the generic append-only evidence log used by
Nika Core foundation services. This document describes the integrity contract for
that log. Product-specific evidence stores may add stronger commitments, but they
must not weaken the generic contract when they use `AuditLog`.

This hardening is intentionally independent from the active M10 authorization work
in PRs #61 and #62. It does not change R0-R4 policy decisions, approval identities,
or UI approval channels.

## Security properties

Every new `AuditLog.append()` or `append_with_connection()` event is persisted with
an internal `_nika_audit_integrity` envelope. The envelope contains:

- a fixed integrity format version;
- the SHA-256 digest of the previous audit event;
- the SHA-256 digest of the current event.

The current digest binds all persisted evidence fields:

- `event_id`;
- `event_type`;
- `entity_type`;
- `entity_id`;
- the redacted user payload;
- `created_at`;
- the previous digest.

Canonical JSON uses UTF-8, sorted keys, compact separators, and rejects non-finite
JSON numbers. Event IDs must remain contiguous. The `sqlite_sequence` value must
match the persisted tail, so deleting the last event is also detected.

`list_for()` verifies the complete global audit chain in one SQLite read snapshot
before returning entity history. A corrupted chain therefore fails closed instead
of returning evidence that appears valid.

## Atomic append protocol

`append_with_connection()` preserves caller-owned transaction semantics.

1. Validate event identifiers and reject attempts to supply the reserved integrity
   envelope.
2. Recursively redact secret-bearing payload keys before any database write.
3. Canonicalize the redacted payload before any database write.
4. Ensure an outer SQLite transaction exists and create an internal savepoint.
5. Insert the provisional redacted row. This acquires SQLite's writer lock before
   reading the chain, serializing concurrent writers without a custom lock service.
6. Verify every prior event and the sequence continuity while the writer lock is
   held.
7. Compute and persist the new chain seal.
8. Release the savepoint.

Any integrity failure rolls back to the savepoint before the exception escapes.
Even if a caller catches that exception and later commits its outer transaction,
the provisional event cannot be committed.

## Legacy compatibility

Existing unsealed events are accepted only as a contiguous legacy prefix. The first
new sealed event commits to the computed digest of that prefix, so later changes to
legacy rows are detected by the sealed descendant.

Once integrity activation has occurred, any later unsealed event is rejected. This
makes direct SQL writes after activation visible as a fail-closed integrity failure.

A database containing only legacy events can be verified structurally, but there is
no pre-existing cryptographic commitment to authenticate the contents of that
legacy-only history.

## Secret minimization

Before persistence, payload values are replaced with `[REDACTED]` for known
credential-bearing keys and suffixes, including password, secret, API key, token,
authorization, cookie, session, credential, and private-key forms.

Redaction happens before both provisional persistence and digest calculation.
Integrity error messages identify only event IDs or structural failures; they do not
include payload values.

Callers must still avoid putting secrets in audit payloads. Redaction is a
defense-in-depth boundary, not permission to log arbitrary credentials.

## Threat model and limitation

The SHA-256 chain is **tamper-evident**, not an authenticated signature. It detects
corruption, deletion, direct unsealed writers, and edits that do not also rewrite
the complete chain.

It does not protect against an attacker with unrestricted write access to the
entire SQLite database who can rewrite every event, every digest, and sequence
metadata consistently. Adding authenticated remote anchoring or key-backed
signatures would require an explicit key-management and recovery design; this
batch does not invent one.

## REUSE -> ADAPT -> CUSTOM decision

- **REUSE:** SQLite transaction, writer-lock, savepoint, AUTOINCREMENT sequence,
  Python `hashlib`, `hmac.compare_digest`, and canonical JSON primitives.
- **ADAPT:** the repository's existing Product Factory SHA-256 commitment pattern,
  without importing Product Factory modules into the kernel dependency direction.
- **CUSTOM (thin):** only Nika-specific event framing, reserved envelope semantics,
  legacy-prefix transition, recursive audit redaction, and fail-closed verification.

No new runtime or cryptography dependency is introduced.

## Verification

Focused adversarial coverage is in `tests/test_audit_integrity.py`:

- sealed round trip and integrity report;
- recursive secret redaction with raw-storage assertions;
- reserved-envelope spoof rejection;
- payload, identity, and timestamp tampering;
- middle and tail deletion;
- unsealed direct writer after activation;
- legacy-prefix anchoring and later tamper detection;
- caller transaction rollback;
- savepoint rollback when the caller catches an integrity error;
- concurrent SQLite writers;
- invalid JSON corruption.

Repository acceptance still requires exact-head CI, including the normal Windows
and Linux Core CI jobs. Automated tests do not establish `HUMAN_TESTED` or
`NVDA_VERIFIED`.
