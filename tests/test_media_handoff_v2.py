from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nika_core.media.contracts import (
    AssetKind,
    EngineDescriptor,
    MediaAsset,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    ModelDescriptor,
    OCRDocument,
    OCRPage,
    ProvenanceChain,
    ProvenanceEvent,
    Segment,
    StructuredMediaArtifact,
    TextRevision,
    Transcript,
    TranscriptMethod,
)
from nika_core.media.handoff import (
    build_corpus_media_handoff_v2,
    dump_corpus_media_handoff_v2,
    load_corpus_media_handoff_v2,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_CREATED = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
_OBSERVED = datetime(2026, 8, 23, 18, 5, tzinfo=UTC)
_TRANSCRIBED = datetime(2026, 8, 23, 18, 10, tzinfo=UTC)
_REVISED = datetime(2026, 8, 23, 18, 15, tzinfo=UTC)
_EVENT = datetime(2026, 8, 23, 18, 20, tzinfo=UTC)


def _artifact() -> StructuredMediaArtifact:
    return StructuredMediaArtifact(
        artifact_id="artifact-1",
        version_id="version-1",
        source=MediaSource(
            source_id="source-1",
            kind=MediaSourceKind.REMOTE_MEDIA,
            locator="https://media.example.test/file?X-Amz-Signature=SECRET-EPHEMERAL",
            privacy="sensitive",
            auth_ref="vault:media/session-private",
            created_at=_CREATED,
        ),
        version=MediaVersion(
            version_id="version-1",
            source_id="source-1",
            observed_at=_OBSERVED,
            metadata_sha256=_SHA_A,
            content_sha256=_SHA_B,
            title="Fixture",
            duration_seconds=12.5,
            upstream_id="remote-upstream-identity",
        ),
        assets=(
            MediaAsset(
                asset_id="asset-z",
                version_id="version-1",
                kind=AssetKind.DOCUMENT,
                relative_path="private/cache/source-z.pdf",
                sha256=_SHA_C,
                size_bytes=300,
                media_type="application/pdf",
                immutable_original=False,
            ),
            MediaAsset(
                asset_id="asset-a",
                version_id="version-1",
                kind=AssetKind.ORIGINAL,
                relative_path="private/cache/original.bin",
                sha256=_SHA_B,
                size_bytes=200,
                media_type="application/octet-stream",
                immutable_original=True,
            ),
        ),
        transcript=Transcript(
            transcript_id="transcript-1",
            version_id="version-1",
            method=TranscriptMethod.OFFLINE_ASR,
            language="uk",
            source_track_id="audio-track-1",
            created_at=_TRANSCRIBED,
            segments=(
                Segment(
                    segment_id="segment-1",
                    start_ms=0,
                    end_ms=1500,
                    text="Перша репліка",
                    confidence=0.95,
                ),
            ),
        ),
        ocr_document=OCRDocument(
            document_id="ocr-1",
            version_id="version-1",
            engine_id="tesseract",
            model_id="traineddata-ukr",
            pages=(
                OCRPage(
                    page_number=1,
                    text="OCR сторінка",
                    confidence=0.91,
                    source_sha256=_SHA_C,
                ),
            ),
        ),
        revisions=(
            TextRevision(
                revision_id="revision-0",
                artifact_id="artifact-1",
                ordinal=0,
                text="Нормалізований текст",
                reason="deterministic normalization",
                accepted=True,
                created_at=_REVISED,
            ),
        ),
        engines=(
            EngineDescriptor(
                engine_id="z-engine",
                name="Secondary Engine",
                version="9.1.0",
                license_id="Apache-2.0",
                source_reference="https://engine.example.test/download?token=ENGINE-SECRET",
                executable_sha256=_SHA_D,
                build_configuration="PRIVATE-BUILD-INTERNALS",
            ),
            EngineDescriptor(
                engine_id="tesseract",
                name="Tesseract",
                version="5.5.2",
                license_id="Apache-2.0",
                source_reference="upstream:tesseract",
                executable_sha256=_SHA_C,
            ),
        ),
        models=(
            ModelDescriptor(
                model_id="z-model",
                engine_id="z-engine",
                version="2.0",
                license_reference="private:model-license-url",
                sha256=_SHA_D,
                size_bytes=999,
            ),
            ModelDescriptor(
                model_id="traineddata-ukr",
                engine_id="tesseract",
                version="2026.08",
                license_reference="model-license:ukr",
                sha256=_SHA_A,
                size_bytes=123,
            ),
        ),
        provenance=ProvenanceChain(
            events=(
                ProvenanceEvent(
                    sequence=0,
                    event_type="media.imported",
                    actor="nika.media",
                    input_sha256=(_SHA_A,),
                    output_sha256=(_SHA_B,),
                    details={
                        "private_note": "DO-NOT-HANDOFF",
                        "signed_url": "https://private.example.test/?sig=PROVENANCE-SECRET",
                    },
                    created_at=_EVENT,
                ),
            )
        ),
    )


def test_v2_round_trip_preserves_exact_safe_identity_and_provenance() -> None:
    handoff = build_corpus_media_handoff_v2(_artifact())
    restored = load_corpus_media_handoff_v2(dump_corpus_media_handoff_v2(handoff))

    assert restored == handoff
    assert restored.schema_version == 2
    assert restored.artifact_id == "artifact-1"
    assert restored.source_id == "source-1"
    assert restored.source_kind == "remote_media"
    assert restored.source_created_at == _CREATED
    assert restored.version_id == "version-1"
    assert restored.version_observed_at == _OBSERVED
    assert restored.metadata_sha256 == _SHA_A
    assert restored.content_sha256 == _SHA_B
    assert [item.asset_id for item in restored.assets] == ["asset-a", "asset-z"]
    assert restored.assets[0].sha256 == _SHA_B
    assert restored.transcript is not None
    assert restored.transcript.transcript_id == "transcript-1"
    assert restored.transcript.method == "offline_asr"
    assert restored.transcript.created_at == _TRANSCRIBED
    assert restored.ocr is not None
    assert restored.ocr.engine_id == "tesseract"
    assert restored.ocr.model_id == "traineddata-ukr"
    assert [item.engine_id for item in restored.engines] == ["tesseract", "z-engine"]
    assert [item.model_id for item in restored.models] == ["traineddata-ukr", "z-model"]
    assert restored.provenance[0].created_at == _EVENT
    assert len(restored.provenance[0].event_sha256) == 64
    assert len(restored.artifact_provenance_sha256) == 64
    assert len(restored.handoff_sha256) == 64


def test_v2_strips_ephemeral_credentials_paths_and_engine_specific_internals() -> None:
    serialized = dump_corpus_media_handoff_v2(build_corpus_media_handoff_v2(_artifact()))

    for forbidden in (
        "SECRET-EPHEMERAL",
        "vault:media/session-private",
        "private/cache/source-z.pdf",
        "private/cache/original.bin",
        "ENGINE-SECRET",
        "PRIVATE-BUILD-INTERNALS",
        "private:model-license-url",
        "DO-NOT-HANDOFF",
        "PROVENANCE-SECRET",
        "remote-upstream-identity",
    ):
        assert forbidden not in serialized


def test_v2_does_not_fabricate_missing_transcript_engine_or_model_binding() -> None:
    handoff = build_corpus_media_handoff_v2(_artifact())

    assert handoff.transcript is not None
    transcript_payload = handoff.transcript.model_dump(mode="json")
    assert "engine_id" not in transcript_payload
    assert "model_id" not in transcript_payload
    assert {item.engine_id for item in handoff.engines} == {"tesseract", "z-engine"}
    assert {item.model_id for item in handoff.models} == {"traineddata-ukr", "z-model"}


def test_v2_rejects_duplicate_asset_identity_before_canonicalization() -> None:
    artifact = _artifact()
    duplicate = artifact.assets[0].model_copy(update={"sha256": _SHA_D})
    ambiguous = artifact.model_copy(update={"assets": (*artifact.assets, duplicate)})

    with pytest.raises(ValueError, match="asset identities"):
        build_corpus_media_handoff_v2(ambiguous)


def test_v2_rejects_duplicate_revision_identity() -> None:
    artifact = _artifact()
    duplicate = TextRevision(
        revision_id="revision-0",
        artifact_id="artifact-1",
        parent_revision_id="revision-0",
        ordinal=1,
        text="Другий текст",
        reason="second pass",
        accepted=False,
        created_at=_REVISED,
    )
    ambiguous = artifact.model_copy(update={"revisions": (*artifact.revisions, duplicate)})

    with pytest.raises(ValueError, match="revision identities"):
        build_corpus_media_handoff_v2(ambiguous)


def test_v2_checkpoint_tamper_is_rejected() -> None:
    payload = json.loads(dump_corpus_media_handoff_v2(build_corpus_media_handoff_v2(_artifact())))
    payload["metadata_sha256"] = _SHA_D

    with pytest.raises(ValidationError, match="checksum mismatch"):
        load_corpus_media_handoff_v2(json.dumps(payload))


def test_v2_checkpoint_rejects_extra_workspace_authority() -> None:
    payload = json.loads(dump_corpus_media_handoff_v2(build_corpus_media_handoff_v2(_artifact())))
    payload["workspace_id"] = "caller-fabricated-workspace"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_corpus_media_handoff_v2(json.dumps(payload))


def test_v2_checkpoint_rejects_wrong_schema_version() -> None:
    payload = json.loads(dump_corpus_media_handoff_v2(build_corpus_media_handoff_v2(_artifact())))
    payload["schema_version"] = 1

    with pytest.raises(ValidationError):
        load_corpus_media_handoff_v2(json.dumps(payload))


def test_v2_checkpoint_rejects_ocr_model_engine_tamper_even_with_recomputed_shape() -> None:
    payload = json.loads(dump_corpus_media_handoff_v2(build_corpus_media_handoff_v2(_artifact())))
    assert payload["ocr"] is not None
    payload["ocr"]["engine_id"] = "z-engine"

    with pytest.raises(ValidationError, match="OCR model/engine identity mismatch"):
        load_corpus_media_handoff_v2(json.dumps(payload))


def test_artifact_provenance_digest_changes_with_material_identity() -> None:
    original = _artifact()
    baseline = build_corpus_media_handoff_v2(original).artifact_provenance_sha256

    changed_version = original.version.model_copy(update={"metadata_sha256": _SHA_D})
    changed = original.model_copy(update={"version": changed_version})
    assert build_corpus_media_handoff_v2(changed).artifact_provenance_sha256 != baseline

    changed_asset = original.assets[0].model_copy(update={"sha256": _SHA_D})
    changed = original.model_copy(update={"assets": (changed_asset, original.assets[1])})
    assert build_corpus_media_handoff_v2(changed).artifact_provenance_sha256 != baseline

    changed_engine = original.engines[1].model_copy(update={"version": "5.5.3"})
    changed = original.model_copy(update={"engines": (original.engines[0], changed_engine)})
    assert build_corpus_media_handoff_v2(changed).artifact_provenance_sha256 != baseline


def test_v2_canonical_identity_order_is_stable() -> None:
    artifact = _artifact()
    first = build_corpus_media_handoff_v2(artifact)
    reordered = artifact.model_copy(
        update={
            "assets": tuple(reversed(artifact.assets)),
            "engines": tuple(reversed(artifact.engines)),
            "models": tuple(reversed(artifact.models)),
        }
    )
    second = build_corpus_media_handoff_v2(reordered)

    assert second.assets == first.assets
    assert second.engines == first.engines
    assert second.models == first.models
    assert second.artifact_provenance_sha256 == first.artifact_provenance_sha256
    assert second.handoff_sha256 == first.handoff_sha256
