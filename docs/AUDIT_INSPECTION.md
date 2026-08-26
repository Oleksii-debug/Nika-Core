# Audit inspection contract

## Status

This document defines the first production inspection surface over the existing canonical
`audit_events` table and `nika_core.kernel.audit.AuditLog`.

It does not create a second audit store, change the SQLite schema, or replace existing append
semantics. `AuditLog.append`, `append_with_connection`, and `list_for` remain the internal raw
evidence interfaces used by existing code.

## User-facing requirement

`docs/MASTER_SPEC.md` requires Nika Core to let the user inspect state, logs, audit evidence and
artifacts. Raw unbounded SQLite reads are not an acceptable presentation contract: they can consume
unbounded memory, produce unstable pages under concurrent appends, and accidentally expose values
that should never be repeated into UI text or support output.

The user-facing audit path therefore uses `AuditLog.inspect()` with `AuditInspectionQuery`.

## Query contract

`AuditInspectionQuery` supports exact optional filters for:

- `event_type`;
- `entity_type`;
- `entity_id`.

Pagination is forward-only and deterministic by monotonically increasing `event_id`:

1. first page uses `after_event_id=0`;
2. the next page uses the last returned `event_id` as `after_event_id`;
3. rows are always returned in ascending `event_id` order.

The default page size is 100 and the hard maximum is 500. Zero, negative or oversized limits and
negative cursors fail closed. Non-integer cursors/limits, booleans used as integers, non-string
filters and blank supplied filters also fail closed. SQL column names are selected only from the
fixed implementation allowlist; all values use SQLite parameters.

This is intentionally cursor-style pagination rather than offset pagination. Concurrent appends can
appear only on later pages and cannot shift already-consumed rows backward or cause offset-based
re-reading.

## Inspection payload safety

`AuditLog.inspect()` returns `AuditInspectionEvent`, not raw `AuditEvent`. The projection recursively
redacts common secret-bearing keys, including API keys/hashes, passwords/passphrases, bearer or
authorization values, all `*_token` authority values, cookies, client secrets, private keys and
credential handles. It also minimizes common credential leakage embedded in strings:

- `Authorization:`, `Proxy-Authorization:`, `Cookie:` and `Set-Cookie:` header values;
- Bearer tokens;
- inline password/API-key/token/client-secret/private-key forms;
- private-key PEM blocks;
- embedded HTTP/HTTPS URL userinfo;
- common secret or OAuth-code query/fragment parameters.

Malformed HTTP/HTTPS URLs that cannot be parsed safely are replaced with `[REDACTED_URL]` rather
than returned unchanged.

Stable non-secret references such as `credential_id` are intentionally retained because they are
audit/provenance identifiers and are required to explain which bounded credential reference was
used. Authority-bearing handles such as `credential_handle` are redacted.

Redaction is defense in depth, not permission to log secrets. Producers remain responsible for
never putting raw persistent credentials, cookies, tokens or private material into audit payloads.
A novel secret encoded under an unrelated key or arbitrary free-form text cannot be proven safe by
pattern matching alone. New user-facing audit consumers must use `inspect()` rather than
`list_for()` unless they are explicitly operating inside a trusted internal boundary that requires
raw evidence.

## Integrity behavior

Persisted audit payloads are required to be JSON objects. Inspection fails closed with
`AuditIntegrityError` if a selected row has malformed JSON or a non-object payload instead of
silently skipping, coercing or partially presenting corrupted evidence.

No migration is needed because the existing `audit_events(event_id, event_type, entity_type,
entity_id, payload_json, created_at)` schema already contains the required ordering/filter data.

## Accessibility

The contract is presentation-neutral and returns ordered text/data primitives. A future WebView2 or
CLI consumer can expose the sequence with ordinary semantic controls or plain text without parsing
visual layout. This change does not itself constitute a human NVDA test.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE:** `SQLiteStore`, the existing `audit_events` table, existing `AuditLog` append authority,
  SQLite parameter binding and monotonically increasing `event_id`.
- **ADAPT:** the existing exact-entity read path into bounded filtered forward inspection without a
  schema fork.
- **CUSTOM (thin):** query validation, cursor semantics, fail-closed decode and a deterministic
  secret-minimized presentation projection.

No new dependency, generic logging framework, database, workflow permission or release authority is
introduced.
