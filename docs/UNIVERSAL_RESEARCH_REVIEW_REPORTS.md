# Universal Research review, cards, and accessible report contract

Updated: 2026-08-20.
Scope: DEV01 Universal Research / Knowledge Corpus only.

## Capability boundary

This batch closes the backend/model boundary between persisted research results and future workspace/UI presentation. It does not add UI controls, browser automation, OCR, vector retrieval, or model calls.

The deterministic path is:

`persisted ResearchResultSet -> structured ResearchCard projection -> durable review projection -> accessible plain-text report`

`ResearchCardService` consumes the existing persisted result/evidence contract. It preserves result ordering, title, snippet, rank, deterministic `why_matched`, all scoped provenance, and current review state.

## Review identity and durability

Review state belongs to `(workspace_id, document_id)`, not to a transient result-set row. Therefore a decision survives recurring reruns that rediscover the same normalized corpus document.

States are intentionally small and deterministic:

- `unreviewed` — implicit default or explicit reset;
- `saved` — retained for follow-up;
- `dismissed` — reviewed and not retained.

Optional review notes are bounded to 4,000 characters.

No second database or state kernel is introduced. Changes are append-only events in the existing canonical `audit_events` table with event type `research.review.changed`. The latest matching event is the current projection. Repeating an identical state+note write is idempotent and does not create audit churn.

Workspace isolation fails closed: a document cannot be reviewed through a workspace that does not own that corpus document. Unknown document identities fail instead of manufacturing state.

## Accessible report foundation

The report model is plain deterministic text and structured cards. It uses explicit labels rather than color or layout. Every result includes, where present:

- result number and title;
- review state and note;
- numeric rank;
- deterministic match explanation;
- summary/snippet;
- each evidence source ID, source kind, freshness, locator, and observation time.

This text model is the safe source for future shared TXT/HTML/DOCX/XLSX renderers. Those renderers remain a separate shared-report/UI integration concern and must not be implemented by DEV01 by editing shared UI ownership.

## REUSE / ADAPT / CUSTOM

- **REUSE** — canonical Nika SQLite and existing `audit_events`; existing `ResearchResultSet`, evidence, normalized corpus document identity, and result ordering.
- **ADAPT** — none required for this batch.
- **CUSTOM (thin)** — Nika-specific research review semantics, structured card projection, and accessible deterministic report projection.
- **No new dependency** — standard library `dataclasses`, `enum`, `hashlib`, and `json` only.

## Acceptance evidence required

Automated tests must prove:

1. implicit `unreviewed` state;
2. saved review survives a new repository/store instance (restart projection);
3. identical writes do not duplicate audit events;
4. state transitions record previous state;
5. unknown and cross-workspace documents fail closed;
6. note bound is enforced;
7. cards preserve result order, review, and provenance;
8. accessible text contains explicit review/provenance labels and Unicode/Ukrainian content.

Human/NVDA credit is not applicable to this backend-only batch. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
