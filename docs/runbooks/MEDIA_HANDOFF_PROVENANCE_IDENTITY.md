# Media handoff provenance identity

Owner: MANUAL-DEV25 media → Corpus handoff boundary.

## Problem

`validate_artifact_for_handoff()` previously converted engine evidence to a set of `engine_id` values and model evidence to a dictionary keyed by `model_id`.

That made lookup convenient but silently collapsed ambiguous evidence. Two `EngineDescriptor` records with the same `engine_id`, or two `ModelDescriptor` records with the same `model_id`, could reach the handoff validator and one identity would effectively hide the other. A conflicting model record could therefore be selected according to tuple/dict order rather than rejected as ambiguous provenance.

The same boundary generated Corpus text-block identities directly from transcript `segment_id` and OCR `page_number`. The upstream transcript/OCR contracts do not independently require those identities to be unique, so a valid `StructuredMediaArtifact` could otherwise produce duplicate `transcript:<segment_id>` or `ocr:page:<page_number>` block identities in one handoff.

`TextRevision.revision_id` also has no upstream uniqueness validator. A revision chain could therefore contain two contiguous revisions with the same ID, including a later accepted revision whose parent and own identity are both the earlier ID. V1 serializes the accepted `revision_id`, so that duplicate would make the downstream accepted-revision identity ambiguous even though ordinal and immediate-parent checks pass.

The downstream Corpus handoff intentionally does not expose engine-specific details, so ambiguity must be rejected before that information is stripped or duplicated block/revision identities are emitted.

## Fail-closed invariant

Before OCR evidence lookup or handoff construction:

1. every transcript `segment_id` used as a Corpus text-block identity must be unique within the transcript;
2. every OCR `page_number` used as a Corpus text-block identity must be unique within the OCR document;
3. every `EngineDescriptor.engine_id` in a `StructuredMediaArtifact` must be unique;
4. every `ModelDescriptor.model_id` must be unique;
5. every model's `engine_id` must resolve to exactly one engine evidence record in the same artifact;
6. OCR document engine/model references retain their existing exact-identity and engine↔model compatibility checks;
7. every `TextRevision.revision_id` must be unique before the accepted V1 revision identity can be emitted.

Duplicate records are rejected even when their remaining fields are identical. Identity uniqueness is the contract; consumers must never infer which duplicate is authoritative.

OCR-specific compatibility errors remain ordered before the global model→engine closure: an OCR model that exists but belongs to a different engine still fails with the established `OCR model and OCR engine identity mismatch`, while an otherwise unreferenced model whose engine evidence is absent fails the global closed-graph check.

Asset-ID uniqueness is deliberately not added to the V1 contract by this repair because V1 does not serialize asset identities. The additive V2 envelope owns canonical asset identity/provenance semantics. This keeps the V1 repair limited to identities actually exposed downstream.

## REUSE → ADAPT → CUSTOM(thin)

- REUSE the existing `StructuredMediaArtifact`, transcript/OCR/revision contracts, engine/model descriptors and DEV05→DEV01 handoff schema.
- ADAPT the current handoff validator into a unique closed evidence graph before lookup, block construction and accepted-revision emission.
- CUSTOM(thin) only the Nika provenance invariant that ambiguous evidence/block/revision identities fail closed.

No new store, schema migration, dependency, framework, credential surface, model, binary, network access or permission is added.

## Compatibility

The serialized `CorpusMediaHandoffV1` schema is unchanged. Valid artifacts with unique evidence, block-source and revision identities behave exactly as before. Only artifacts whose provenance was ambiguous, internally incomplete, would generate duplicate Corpus block identities, or reuse a V1 revision identity are newly rejected.

Focused regression: `tests/test_media_handoff_provenance_identity.py`.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
