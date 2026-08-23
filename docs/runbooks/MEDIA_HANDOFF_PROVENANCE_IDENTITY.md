# Media handoff provenance identity

Owner: MANUAL-DEV25 media → Corpus handoff boundary.

## Problem

`validate_artifact_for_handoff()` previously converted engine evidence to a set of `engine_id` values and model evidence to a dictionary keyed by `model_id`.

That made lookup convenient but silently collapsed ambiguous evidence. Two `EngineDescriptor` records with the same `engine_id`, or two `ModelDescriptor` records with the same `model_id`, could reach the handoff validator and one identity would effectively hide the other. A conflicting model record could therefore be selected according to tuple/dict order rather than rejected as ambiguous provenance.

The downstream Corpus handoff intentionally does not expose engine-specific details, so ambiguity must be rejected before that information is stripped.

## Fail-closed invariant

Before OCR evidence lookup or handoff construction:

1. every `EngineDescriptor.engine_id` in a `StructuredMediaArtifact` must be unique;
2. every `ModelDescriptor.model_id` must be unique;
3. every model's `engine_id` must resolve to exactly one engine evidence record in the same artifact;
4. OCR document engine/model references retain their existing exact-identity and engine↔model compatibility checks.

Duplicate records are rejected even when their remaining fields are identical. Identity uniqueness is the contract; consumers must never infer which duplicate is authoritative.

## REUSE → ADAPT → CUSTOM(thin)

- REUSE the existing `StructuredMediaArtifact`, engine/model descriptors and DEV05→DEV01 handoff schema.
- ADAPT the current handoff validator into a unique closed evidence graph before lookup.
- CUSTOM(thin) only the Nika provenance invariant that ambiguous evidence identities fail closed.

No new store, schema migration, dependency, framework, credential surface, model, binary, network access or permission is added.

## Compatibility

The serialized `CorpusMediaHandoffV1` schema is unchanged. Valid artifacts with unique evidence identities behave exactly as before. Only artifacts whose provenance was ambiguous or internally incomplete are newly rejected.

Focused regression: `tests/test_media_handoff_provenance_identity.py`.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
