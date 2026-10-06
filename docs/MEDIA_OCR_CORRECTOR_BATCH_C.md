# DEV05 Batch C — OCR + Corrector

Starting main: `4e5b03b7ca0f58bf907f7c69b6966084b2da88c0`.

## Scope

This batch extends the existing durable `nika_core.media` subsystem. It does not create a second document/corpus system and does not edit DEV01 extraction/search or DEV04 interaction/vision policy.

### OCR baseline

- `TesseractOCRAdapter` is an optional external-engine adapter behind Nika-owned `OCREnginePort`/`OCRPageRequest` contracts.
- It invokes an already installed/discovered executable through `SafeProcessRunner` with typed argv, `shell=False`, stdin disabled, output bounds, timeout and process-tree cancellation inherited from the Batch A subprocess boundary.
- Nika never installs Tesseract or language packs from ordinary OCR execution. Missing executable/runtime is an explicit `component_missing` error.
- Language selection is validated and passed as one argv value; arbitrary command strings are not accepted.
- Tesseract TSV is normalized to Nika `OCRPage` text/confidence contracts. Upstream objects never cross the domain boundary.
- The adapter can report executable checksum when an explicit executable file is used. Engine license remains distinct from language/model data license.

Current upstream verification for this implementation cycle: Tesseract 5.5.2 is the latest release observed from the official upstream releases page. Tesseract source is Apache-2.0; upstream documents Leptonica as BSD-2-Clause-like. Language traineddata remains separately attributable and is not bundled by this batch.

### Durable pages and restart

Media schema v3 adds `media_ocr_pages` inside the existing canonical Nika SQLite database. Page state is durable. A page that was RUNNING during process loss is restored to PENDING with explicit restart-reconciliation evidence. COMPLETED pages are immutable and skipped rather than silently reprocessed.

### Corrector

`normalize_text()` is deterministic and model-free: Unicode NFC, line-ending normalization, bounded whitespace cleanup and protected-term preservation. `RevisionCorrector` appends `TextRevision` records through the existing media repository. Original evidence is not overwritten. Accepted/rejected revision state remains explicit.

No ModelGateway semantic rewrite is introduced in this batch. A future semantic suggestion may use the existing ModelGateway only as a suggestion source; it must never replace original text without an accepted append-only revision.

### Deferred by evidence

- OpenCV preprocessing remains deferred until fixture CER/confidence measurements prove a repeatable gain.
- PaddleOCR remains an optional heavy candidate and is not added to base dependencies.
- No claim is made that Tesseract beats PaddleOCR or any vision model on the target machine.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
