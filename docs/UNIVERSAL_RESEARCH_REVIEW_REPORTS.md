# Universal Research review, cards, and accessible report contract

Updated: 2026-08-20.
Scope: DEV01 Universal Research / Knowledge Corpus only.

## Capability boundary

This contract closes the backend/model boundary between persisted research results and future workspace/UI presentation. It does not add UI controls, browser automation, OCR, vector retrieval, or model calls.

The deterministic path is:

`persisted ResearchResultSet -> structured ResearchCard projection -> durable review projection -> accessible report -> format renderer`

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

## Multi-format export

`ResearchReportExporter` renders the same structured report without additional fetching, extraction, OCR, LLM work, or UI state. Supported formats are TXT, CSV, semantic HTML, DOCX, and XLSX.

Export does not accept a filesystem path. It returns a generated leaf filename, media type, and bytes so path authorization remains the caller/runtime's responsibility rather than being bypassed inside the research domain. Result-set IDs are reduced to a safe leaf filename component.

TXT is UTF-8 and preserves the canonical accessible text report. CSV and XLSX use one flat row per result evidence item, repeating result metadata where needed so provenance is not lost; a result with no evidence still has one row with blank evidence fields. Spreadsheet cells derived from strings beginning with formula trigger characters (`=`, `+`, `-`, `@`, including after leading whitespace) are prefixed with an apostrophe to prevent formula injection.

HTML uses semantic headings, definition lists, ordered evidence lists, UTF-8 metadata, and escaping for all user/source text. DOCX uses real heading levels and explicit labelled paragraphs rather than visual-only formatting. XLSX uses separate `Metadata` and flat `Results` sheets, a header row, frozen headers, and no merged cells.

Office package bytes are produced by the already-adopted `python-docx` and `openpyxl` dependencies. The semantic content is deterministic; byte-for-byte DOCX/XLSX identity is not claimed because ZIP/package metadata may vary across renders.

## REUSE / ADAPT / CUSTOM

- **REUSE** — canonical Nika SQLite and existing `audit_events`; existing `ResearchResultSet`, evidence, normalized corpus document identity, and result ordering; already-adopted `python-docx` and `openpyxl` libraries.
- **ADAPT** — standard-library CSV/HTML/IO primitives and the existing structured report are adapted into accessible export formats.
- **CUSTOM (thin)** — Nika-specific research review semantics, structured card projection, safe flat provenance rows, spreadsheet-injection guard, and format projection.
- **No new dependency or migration** — export reuses dependencies already present for document ingestion and introduces no schema/state kernel.

## Acceptance evidence required

Automated tests must prove:

1. implicit `unreviewed` state;
2. saved review survives a new repository/store instance (restart projection);
3. identical writes do not duplicate audit events;
4. state transitions record previous state;
5. unknown and cross-workspace documents fail closed;
6. note bound is enforced;
7. cards preserve result order, review, and provenance;
8. accessible text contains explicit review/provenance labels and Unicode/Ukrainian content;
9. TXT round-trips UTF-8 canonical report text;
10. CSV and XLSX preserve provenance while preventing formula injection;
11. HTML escapes source/user markup and retains semantic headings/evidence structure;
12. DOCX can be reopened by `python-docx` with headings, review state, and provenance intact;
13. generated export filenames cannot contain caller-controlled path separators.

Human/NVDA credit is not applicable to this backend export batch. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
