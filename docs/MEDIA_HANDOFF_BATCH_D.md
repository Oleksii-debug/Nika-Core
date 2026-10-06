# DEV05 Batch D — Structured Media delivery and corpus handoff

Status: implementation candidate. This document does not claim GREEN, HUMAN_TESTED, NVDA_VERIFIED, or real-engine proof.

## Purpose

DEV05 owns the durable media evidence system. DEV01 owns corpus/search/indexing. The boundary between them is `CorpusMediaHandoffV1`; DEV01 must not depend on yt-dlp, FFmpeg, Tesseract, faster-whisper, sherpa-onnx, PaddleOCR, or any other media-engine object.

The handoff contains stable media/source/version identities, privacy class, content checksum, text blocks with timing or page locus, the latest accepted correction revision, and redacted provenance references. Engine/model descriptors remain inside the `StructuredMediaArtifact` evidence and are intentionally excluded from the corpus handoff payload.

## OCR request boundary

`OCRInputRequestV1` is the reverse DEV01 -> DEV05 request contract. It accepts only stable source/version/asset identities, an optional sorted page set, and one of two explicit reasons:

- `text_layer_missing`;
- `text_layer_insufficient`.

The schema has no URL, cookie, token, browser-profile, local-path, arbitrary command, or engine-specific option field. DEV05 resolves the referenced immutable asset from `MediaRepository`. Only original/document assets are eligible for OCR.

## Revision truth

`StructuredMediaArtifact` is immutable evidence. Corrector revisions are separately append-only in `media_text_revisions`. `MediaDeliveryCoordinator` therefore rehydrates the artifact from the revision ledger immediately before delivery. A conflicting embedded revision snapshot fails closed. The corpus handoff carries only the latest accepted revision; unaccepted suggestions never replace accepted text.

## Provenance and identity validation

Before delivery DEV05 verifies:

- every asset belongs to the artifact version;
- transcript and OCR document belong to the same version;
- OCR engine/model references exist in the artifact evidence and agree with each other;
- text revisions form one contiguous parent chain;
- handoff text blocks have exactly one locus: timestamp range or page number.

Only event type plus input/output SHA-256 references cross the corpus boundary. Provenance free-form details do not.

## Accessible presenter

`render_accessible_media_text()` produces plain copyable text with explicit sections for text blocks, accepted revision, engine/model evidence, provenance, and errors. Timed blocks use textual timestamps and OCR blocks use textual page numbers. There is no color-only or visual-position-only state.

## Deferred / separate proof

This batch does not add a new ASR/OCR engine, download a model, bundle FFmpeg/Tesseract, or choose a transcription winner. OpenCV preprocessing remains conditional on measured fixture CER/confidence. PaddleOCR remains optional heavy. Real-engine Windows proofs and resource measurements remain separate evidence.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false` until real human testing proves otherwise.
