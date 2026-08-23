from __future__ import annotations

import pytest

from nika_core.media.contracts import (
    EngineDescriptor,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    ModelDescriptor,
    OCRDocument,
    OCRPage,
    Segment,
    StructuredMediaArtifact,
    Transcript,
    TranscriptMethod,
)
from nika_core.media.handoff import build_corpus_media_handoff, validate_artifact_for_handoff

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _engine(engine_id: str = "tesseract", *, version: str = "5.5.2") -> EngineDescriptor:
    return EngineDescriptor(
        engine_id=engine_id,
        name=engine_id,
        version=version,
        license_id="Apache-2.0",
        source_reference=f"upstream:{engine_id}",
    )


def _model(
    model_id: str = "traineddata-ukr",
    *,
    engine_id: str = "tesseract",
    version: str = "fixture-v1",
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        engine_id=engine_id,
        version=version,
        license_reference=f"model-license:{model_id}:{version}",
        sha256=_SHA_A,
    )


def _artifact(**updates) -> StructuredMediaArtifact:
    source = MediaSource(
        source_id="source-1",
        kind=MediaSourceKind.LOCAL_FILE,
        locator="media/document.pdf",
        privacy="private",
    )
    version = MediaVersion(
        version_id="version-1",
        source_id=source.source_id,
        metadata_sha256=_SHA_A,
        content_sha256=_SHA_B,
    )
    ocr = OCRDocument(
        document_id="ocr-1",
        version_id=version.version_id,
        engine_id="tesseract",
        model_id="traineddata-ukr",
        pages=(
            OCRPage(
                page_number=1,
                text="Перевірений OCR текст",
                confidence=0.9,
                source_sha256=_SHA_B,
            ),
        ),
    )
    base = StructuredMediaArtifact(
        artifact_id="artifact-1",
        version_id=version.version_id,
        source=source,
        version=version,
        ocr_document=ocr,
        engines=(_engine(),),
        models=(_model(),),
    )
    return base.model_copy(update=updates)


def test_handoff_rejects_duplicate_engine_identity_before_evidence_lookup() -> None:
    artifact = _artifact(
        engines=(
            _engine(version="5.5.2"),
            _engine(version="5.6.0"),
        )
    )

    with pytest.raises(ValueError, match="duplicate media engine identity: tesseract"):
        validate_artifact_for_handoff(artifact)


def test_handoff_rejects_duplicate_model_identity_before_dict_collapse() -> None:
    artifact = _artifact(
        engines=(
            _engine(),
            _engine("alternate-ocr"),
        ),
        models=(
            _model(engine_id="tesseract", version="v1"),
            _model(engine_id="alternate-ocr", version="v2"),
        ),
    )

    with pytest.raises(ValueError, match="duplicate media model identity: traineddata-ukr"):
        validate_artifact_for_handoff(artifact)


def test_handoff_preserves_ocr_model_engine_mismatch_semantics() -> None:
    artifact = _artifact(models=(_model(engine_id="different-engine"),))

    with pytest.raises(ValueError, match="OCR model and OCR engine identity mismatch"):
        validate_artifact_for_handoff(artifact)


def test_handoff_rejects_unreferenced_model_with_missing_engine_evidence() -> None:
    artifact = _artifact(
        models=(
            _model(),
            _model("unused-model", engine_id="missing-engine"),
        )
    )

    with pytest.raises(ValueError, match="references an engine missing from artifact evidence"):
        validate_artifact_for_handoff(artifact)


def test_handoff_rejects_duplicate_transcript_segment_identity() -> None:
    transcript = Transcript(
        transcript_id="transcript-1",
        version_id="version-1",
        method=TranscriptMethod.OFFLINE_ASR,
        segments=(
            Segment(segment_id="same", start_ms=0, end_ms=100, text="first"),
            Segment(segment_id="same", start_ms=100, end_ms=200, text="second"),
        ),
    )
    artifact = _artifact(transcript=transcript)

    with pytest.raises(ValueError, match="transcript segment identities must be unique"):
        build_corpus_media_handoff(artifact)


def test_handoff_rejects_duplicate_ocr_page_identity() -> None:
    ocr = OCRDocument(
        document_id="ocr-1",
        version_id="version-1",
        engine_id="tesseract",
        model_id="traineddata-ukr",
        pages=(
            OCRPage(page_number=1, text="first", source_sha256=_SHA_A),
            OCRPage(page_number=1, text="second", source_sha256=_SHA_B),
        ),
    )
    artifact = _artifact(ocr_document=ocr)

    with pytest.raises(ValueError, match="OCR page identities must be unique"):
        build_corpus_media_handoff(artifact)


def test_handoff_accepts_unique_closed_engine_model_evidence_graph() -> None:
    artifact = _artifact()

    validate_artifact_for_handoff(artifact)
    handoff = build_corpus_media_handoff(artifact)

    assert handoff.artifact_id == artifact.artifact_id
    assert handoff.version_id == artifact.version_id
    assert [block.text for block in handoff.blocks] == ["Перевірений OCR текст"]
