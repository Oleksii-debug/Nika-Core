# Media OCR immutable source binding — P10-06

## Purpose

This repair extends the integrated DEV05 Batch C OCR implementation from PR #66. It does not add a
second OCR framework, OCR engine, Corpus path, or model runtime. The repaired boundary makes the
durable OCR page identity bind the exact source bytes that may be recognized or retried.

## Root defect

Batch C persisted `source_path` for durable OCR pages but did not persist the source SHA-256 before
execution. After a crash or restart, the same `page_id` could therefore be retried against different
bytes placed at the same path. The Tesseract adapter also computed `OCRPage.source_sha256` after the
external process returned, which did not prove that the recognized bytes and recorded provenance
belonged to the same source generation.

## Repair contract

`OCRPageRepository.put()` now binds a newly registered page to a canonical resolved local path and
SHA-256 before it becomes durable. The hash is stored inside the existing `page_json`; media schema
v3 does not need another migration.

The bound source identity is immutable for the lifetime of the durable page. State transitions may
reuse the stored hash but may not replace it. A completed OCR result must carry the same source
SHA-256 as the durable page.

`pending_for_resume()` verifies the bound source before returning work:

- unchanged source bytes may resume;
- a source that disappeared becomes a durable failed page and is not returned for retry;
- substituted bytes become a durable checksum failure and are not returned for retry;
- legacy nonterminal rows that predate source binding fail closed rather than inventing provenance.
  They must be explicitly requeued under a new page identity.

The public OCR execution request requires the expected source SHA-256. `DurableOCRPage.as_request()`
is the canonical conversion from durable page state to execution input.

## Tesseract execution boundary

Before starting Tesseract, the adapter verifies the live source against the expected SHA-256. It
then copies the source into a temporary Nika-owned recognition snapshot beneath the supplied process
working directory and verifies the snapshot digest before execution. Tesseract receives the snapshot
path rather than the caller-owned source path.

After the subprocess returns, the adapter verifies that both the recognition snapshot and original
bound source still match the expected digest. Any mismatch discards the result with
`MediaErrorCode.CHECKSUM_MISMATCH`. The temporary snapshot is removed by the context-managed
temporary directory.

This is a provenance and restart boundary. It does not claim that an external Tesseract binary is
trusted code, and it does not replace existing `SafeProcessRunner` timeout, output-bound,
cancellation, and process-tree cleanup controls.

## REUSE → ADAPT → CUSTOM(thin)

- **REUSE:** integrated PR #66 OCR contracts/repository, `SafeProcessRunner`, `sha256_file`,
  `MediaErrorCode.CHECKSUM_MISMATCH`, canonical SQLite/media schema, and Python temporary-file
  primitives.
- **ADAPT:** durable page registration/restart reconciliation and Tesseract input handling so both
  consume one immutable source-byte identity.
- **CUSTOM(thin):** only the source-binding checks, deterministic reconciliation failure state, and
  focused regressions.

No dependency, model, language pack, binary, workflow, permission, shared migration, Corpus handoff,
ASR, or UI contract is added.

## Evidence boundary

Deterministic tests use synthetic byte fixtures and fake subprocess runners. They cover initial
binding, Unicode/space paths, restart substitution, legacy unbound restart, wrong expected digest,
mutation during OCR, temporary-snapshot cleanup, and same-page concurrent registration.

These tests do **not** prove physical Tesseract inference, OCR quality, a specific language pack, or
target-machine performance.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`PHYSICAL_TESSERACT_VERIFIED=false`
