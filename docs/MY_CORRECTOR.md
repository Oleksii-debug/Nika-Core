# My Corrector — deterministic core contract

Status: production-intended isolated foundation. It is not yet a packaged Product Journey and does not
claim HUMAN_TESTED or NVDA_VERIFIED.

## Purpose

My Corrector is the reusable Nika text-correction workspace. The deterministic core works with no LLM,
no network and no provider credentials. It owns profile/rule semantics, protected terms, privacy-safe
change evidence, revision history, idempotent replay and restart integrity. Semantic rewrite or
explanation is a separate future adapter through the existing `ModelGateway`; this package must never
call a model provider directly.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE:** Python `unicodedata`, `re`, `hashlib`, `json`, `sqlite3`; Nika frozen dataclass and
  fail-closed contract conventions.
- **ADAPT:** Nika's existing `SQLiteStore.connection()` shape. `CorrectorRepository` owns only a
  namespaced `corrector_schema_migrations` ledger and Corrector tables; it does not edit shared schema
  ownership.
- **CUSTOM(thin):** Corrector-specific rule/protected-term/profile contracts, deterministic evidence,
  history and replay integrity.
- **No new dependency:** there is no reason to add a third-party grammar framework to mandatory Core
  for literal deterministic correction. A language-specific engine requires a later measured
  version/license/Windows-fit decision before adoption.

## Deterministic execution

A profile specifies newline normalization, Unicode `NONE`/`NFC`/`NFKC`, ordered literal replacement
rules, and protected terms. Literal rules may be case-sensitive/insensitive and whole-word. Protected
ranges are recalculated before each rule so an earlier replacement cannot silently expose protected
text to a later rule.

Rules run in deterministic `(priority, rule_id)` order. The engine performs a second pass and fails
closed if the output changes again. This makes the public correction operation idempotent for the
actual input instead of silently applying an unstable profile.

## Privacy and evidence

`CorrectionResult.before_text`, `CorrectionResult.after_text`, revision text and profiles are excluded
from dataclass repr output. Durable local session state necessarily contains the user's text so that
editing can resume after restart; that database is local user data, not telemetry.

Portable evidence contains only:
- profile SHA-256;
- input/output SHA-256;
- whether normalization changed text;
- protected-occurrence count;
- per-rule replacement counts keyed by rule ID.

Raw user text, matched fragments, replacements and protected-term values are not included in the
portable evidence JSON. Callers must not copy raw Corrector text into audit/log/provider metadata.

## Durable authority

`CorrectorRepository` accepts any store implementing the existing Nika SQLite `connection()` context
manager, so production may reuse canonical `SQLiteStore` without a new database framework.

The module-owned tables record one session head and immutable contiguous revisions. Writes use
`BEGIN IMMEDIATE`. Each correction carries `(session_id, operation_id, expected_revision,
profile_digest)` authority:
- exact operation replay returns the original durable revision;
- conflicting operation replay fails;
- stale optimistic revision fails;
- session/revision/profile/evidence digests and canonical JSON are revalidated on every read;
- revision numbers and expected revisions must be true SQLite/Python integers, not coercible REALs;
- parent and evidence-input digests form a contiguous lineage;
- timestamps may not rewind;
- session head must equal the final revision.

A no-op correction still creates a durable revision so history truth does not omit attempted
operations.

## Current acceptance boundary

This slice is complete only as the deterministic Corrector core after focused tests plus exact-head
Core/M12 qualification. Full My Corrector user capability additionally needs compatibility work with
current owners for Workspace/Plugin discovery, runtime/permissions, ModelGateway semantic operations,
accessible Windows UI/reporting, packaging and a real Product Journey. Those surfaces are deliberately
not duplicated here.

Automated tests cannot set `HUMAN_TESTED` or `NVDA_VERIFIED`.
