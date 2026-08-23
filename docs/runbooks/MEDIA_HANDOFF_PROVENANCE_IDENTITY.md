# Media handoff provenance identity

Owner: MANUAL-DEV25 media → Corpus handoff boundary.

## Problem

`validate_artifact_for_handoff()` previously converted engine evidence to a set of `engine_id` values and model evidence to a dictionary keyed by `model_id`.

That made lookup convenient but silently collapsed ambiguous evidence. Two `EngineDescriptor` records with the same `engine_id`, or two `ModelDescriptor` records with the same `model_id`, could reach the handoff validator and one identity would effectively hide the other. A conflicting model record could therefore be selected according to tuple/dict order rather than rejected as ambiguous provenance.

The same boundary generated Corpus text-block identities directly from transcript `segment_id` and OCR `page_number`. The upstream transcript/OCR contracts do not independently require those identities to be unique, so a valid `StructuredMediaArtifact` could otherwise produce duplicate `transcript:<segment_id>` or `ocr:page:<page_number>` block identities in one handoff.

The original V1 downstream Corpus handoff intentionally does not expose engine-specific details. That remains the compatibility boundary, so ambiguity must be rejected before information is stripped or duplicate block identities are emitted.

## Fail-closed invariant

Before OCR evidence lookup or handoff construction:

1. every transcript `segment_id` used as a Corpus text-block identity must be unique within the transcript;
2. every OCR `page_number` used as a Corpus text-block identity must be unique within the OCR document;
3. every media `asset_id` used as V2 evidence identity must be unique within the artifact;
4. every `EngineDescriptor.engine_id` in a `StructuredMediaArtifact` must be unique;
5. every `ModelDescriptor.model_id` must be unique;
6. every model's `engine_id` must resolve to exactly one engine evidence record in the same artifact;
7. OCR document engine/model references retain their existing exact-identity and engine↔model compatibility checks;
8. every text revision identity must be unique and the established contiguous parent chain remains mandatory.

Duplicate records are rejected even when their remaining fields are identical. Identity uniqueness is the contract; consumers must never infer which duplicate is authoritative.

OCR-specific compatibility errors remain ordered before the global model→engine closure: an OCR model that exists but belongs to a different engine still fails with the established `OCR model and OCR engine identity mismatch`, while an otherwise unreferenced model whose engine evidence is absent fails the global closed-graph check.

## CorpusMediaHandoffV2

ONE-SHOT-40 adds an additive strict provenance envelope while leaving `CorpusMediaHandoffV1` and its default delivery behavior unchanged.

V2 carries the stable media identity needed for a durable Corpus ingestion decision:

- `artifact_id`, `source_id`, source kind/privacy and source creation timestamp;
- exact `version_id`, version observation timestamp, `metadata_sha256` and optional `content_sha256`;
- canonical asset identity, kind, SHA-256, byte size, media type and immutable-original flag;
- transcript identity, method, language, source-track identity, creation timestamp and transcript digest;
- exact OCR document identity and its `engine_id`/optional `model_id` binding plus document digest;
- canonical engine IDs, names, versions, declared license IDs, executable hashes when available and full-descriptor digests;
- canonical model IDs, exact engine references, versions, model hashes/sizes when available and full-descriptor digests;
- latest accepted revision identity/parent/ordinal/text/reason/timestamp plus full-revision digest;
- provenance sequence/event/actor/input/output hashes/timestamp plus full-event digest;
- `artifact_provenance_sha256`, a deterministic comparison key over validated durable media semantics;
- `handoff_sha256`, a deterministic checkpoint corruption/restart checksum over the complete V2 handoff payload.

`handoff_sha256` is **not** an authority signature. A caller that can construct arbitrary JSON can also calculate a checksum. Corpus-side acceptance must resolve the durable media/source record through the trusted repository boundary and compare the supplied stable identity/provenance to that record. This handoff does not duplicate DEV24 workspace authorization logic and does not accept a caller-supplied `workspace_id` as authority.

The current shared `Transcript` contract has no `engine_id` or `model_id`. V2 therefore does not manufacture an ASR engine/model binding. It carries the exact transcript method/timestamp/digest and the complete validated artifact engine/model evidence graph. Exact ASR binding can be added only when a canonical upstream contract records that relationship.

## Secret and ephemeral data boundary

V2 validates the full media artifact first, then strips values that Corpus does not need to ingest or authenticate the artifact. The serialized envelope does **not** expose:

- source locator or `auth_ref`;
- asset local/relative paths;
- engine source-reference URL or build-configuration internals;
- model license-reference locator;
- provenance `details` values.

Full engine/model/event descriptors remain bound through deterministic SHA-256 fingerprints. A changed descriptor therefore changes `artifact_provenance_sha256` without copying raw engine-specific internals or credential-bearing/signed URLs across the boundary.

## Restart serialization

`dump_corpus_media_handoff_v2()` emits canonical JSON. `load_corpus_media_handoff_v2()` uses `extra="forbid"`, revalidates the model/engine/OCR graph, canonical identity order, provenance sequence and `handoff_sha256` before returning a checkpoint. Wrong schema versions, extra caller authority fields, graph tampering and content/timestamp/hash changes with a stale checksum fail closed.

## REUSE → ADAPT → CUSTOM(thin)

- REUSE the existing `StructuredMediaArtifact`, transcript/OCR contracts, engine/model descriptors, provenance chain and V1 text-block/revision representation.
- ADAPT the validated media evidence graph into a canonical safe V2 envelope without changing DEV05 runtime or DEV24 Corpus authorization ownership.
- CUSTOM(thin): deterministic identity ordering, descriptor/provenance fingerprints and strict restart checksum/loader semantics.

No new store, schema migration, dependency, framework, credential surface, model, binary, network access or permission is added.

## Compatibility and evidence

`CorpusMediaHandoffV1` is unchanged and remains the existing compatibility path. Valid V1 artifacts keep the established engine/model-internals stripping behavior. V2 is explicit and additive.

Focused regressions:

- `tests/test_media_handoff_provenance_identity.py` — duplicate identity/closed evidence graph;
- `tests/test_media_handoff_v2.py` — exact safe provenance, no fabricated ASR binding, canonical order, tamper/restart failure, and secret/ephemeral stripping.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
