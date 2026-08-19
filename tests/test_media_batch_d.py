from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nika_core.data.sqlite import SQLiteStore
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
from nika_core.media.delivery import MediaDeliveryCoordinator
from nika_core.media.handoff import (
    CorpusMediaTextBlockV1,
    MediaTextSourceKind,
    OCRInputRequestV1,
    OCRRequestReason,
    build_corpus_media_handoff,
    validate_artifact_for_handoff,
)
from nika_core.media.presenter import render_accessible_media_text
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import initialize_media_schema


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _source() -> MediaSource:
    return MediaSource(
        source_id="source-1",
        kind=MediaSourceKind.LOCAL_FILE,
        locator="media/original.pdf",
        privacy="private",
    )


def _version() -> MediaVersion:
    return MediaVersion(
        version_id="version-1",
        source_id="source-1",
        metadata_sha256=_SHA_A,
        content_sha256=_SHA_B,
        title="Fixture",
    )


def _asset(*, kind: AssetKind = AssetKind.DOCUMENT, version_id: str = "version-1") -> MediaAsset:
    return MediaAsset(
        asset_id="asset-1",
        version_id=version_id,
        kind=kind,
        relative_path="assets/original.pdf",
        sha256=_SHA_B,
        size_bytes=10,
        media_type="application/pdf",
        immutable_original=True,
    )


def _transcript(*, version_id: str = "version-1") -> Transcript:
    return Transcript(
        transcript_id="transcript-1",
        version_id=version_id,
        method=TranscriptMethod.PLATFORM_SUBTITLE,
        language="uk",
        segments=(
            Segment(segment_id="s1", start_ms=1000, end_ms=2400, text="Перша репліка", confidence=0.9),
            Segment(segment_id="s2", start_ms=2500, end_ms=4000, text="Друга репліка"),
        ),
    )


def _engine() -> EngineDescriptor:
    return EngineDescriptor(
        engine_id="tesseract",
        name="Tesseract",
        version="5.5.2",
        license_id="Apache-2.0",
        source_reference="upstream:tesseract",
        executable_sha256=_SHA_C,
    )


def _model(*, engine_id: str = "tesseract") -> ModelDescriptor:
    return ModelDescriptor(
        model_id="traineddata-ukr",
        engine_id=engine_id,
        version="fixture",
        license_reference="model-license:fixture",
        sha256=_SHA_A,
    )


def _ocr(*, version_id: str = "version-1", engine_id: str = "tesseract", model_id: str | None = None) -> OCRDocument:
    return OCRDocument(
        document_id="ocr-1",
        version_id=version_id,
        engine_id=engine_id,
        model_id=model_id,
        pages=(
            OCRPage(page_number=1, text="Перша сторінка", confidence=0.8, source_sha256=_SHA_B),
            OCRPage(page_number=2, text="Друга сторінка", source_sha256=_SHA_C),
        ),
    )


def _revision(
    ordinal: int,
    *,
    accepted: bool = True,
    parent: str | None = None,
    artifact_id: str = "artifact-1",
) -> TextRevision:
    return TextRevision(
        revision_id=f"revision-{ordinal}",
        artifact_id=artifact_id,
        parent_revision_id=parent,
        ordinal=ordinal,
        text=f"Виправлений текст {ordinal}",
        reason="deterministic normalization",
        accepted=accepted,
    )


def _artifact(**updates) -> StructuredMediaArtifact:
    base = StructuredMediaArtifact(
        artifact_id="artifact-1",
        version_id="version-1",
        source=_source(),
        version=_version(),
        assets=(_asset(),),
        transcript=_transcript(),
        ocr_document=_ocr(),
        engines=(_engine(),),
        provenance=ProvenanceChain(
            events=(
                ProvenanceEvent(
                    sequence=0,
                    event_type="media.imported",
                    actor="nika.media",
                    output_sha256=(_SHA_B,),
                    details={"private_note": "not handed off"},
                ),
            )
        ),
    )
    return base.model_copy(update=updates)


def _repository(tmp_path: Path) -> MediaRepository:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    return MediaRepository(store)


def _persist_base(repository: MediaRepository, artifact: StructuredMediaArtifact | None = None) -> StructuredMediaArtifact:
    current = artifact or _artifact()
    repository.put_source(current.source)
    repository.put_version(current.version)
    for asset in current.assets:
        repository.put_asset(asset)
    repository.put_artifact(current)
    return current


def test_ocr_request_is_versioned_secret_free_identity_only() -> None:
    request = OCRInputRequestV1(
        request_id="request-1",
        source_id="source-1",
        version_id="version-1",
        asset_id="asset-1",
        reason=OCRRequestReason.TEXT_LAYER_MISSING,
        page_numbers=(1, 2),
    )
    assert request.schema_version == 1
    assert set(request.model_dump()) == {
        "schema_version",
        "request_id",
        "source_id",
        "version_id",
        "asset_id",
        "reason",
        "page_numbers",
    }


@pytest.mark.parametrize("pages", [(2, 1), (1, 1), (0,), (1, 3, 2)])
def test_ocr_request_rejects_ambiguous_page_sets(pages: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        OCRInputRequestV1(
            request_id="request-1",
            source_id="source-1",
            version_id="version-1",
            asset_id="asset-1",
            reason=OCRRequestReason.TEXT_LAYER_INSUFFICIENT,
            page_numbers=pages,
        )


def test_handoff_block_requires_exactly_one_locus() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        CorpusMediaTextBlockV1(
            block_id="bad",
            source_kind=MediaTextSourceKind.OCR,
            text="x",
        )
    with pytest.raises(ValidationError, match="exactly one"):
        CorpusMediaTextBlockV1(
            block_id="bad",
            source_kind=MediaTextSourceKind.OCR,
            text="x",
            page_number=1,
            start_ms=0,
            end_ms=1,
        )


def test_handoff_block_rejects_inverted_timing() -> None:
    with pytest.raises(ValidationError, match="end_ms"):
        CorpusMediaTextBlockV1(
            block_id="bad",
            source_kind=MediaTextSourceKind.TRANSCRIPT,
            text="x",
            start_ms=20,
            end_ms=10,
        )


def test_artifact_validation_rejects_foreign_asset_version() -> None:
    artifact = _artifact(assets=(_asset(version_id="other"),))
    with pytest.raises(ValueError, match="assets"):
        validate_artifact_for_handoff(artifact)


def test_artifact_validation_rejects_foreign_transcript_version() -> None:
    artifact = _artifact(transcript=_transcript(version_id="other"))
    with pytest.raises(ValueError, match="transcript"):
        validate_artifact_for_handoff(artifact)


def test_artifact_validation_rejects_foreign_ocr_version() -> None:
    artifact = _artifact(ocr_document=_ocr(version_id="other"))
    with pytest.raises(ValueError, match="OCR document"):
        validate_artifact_for_handoff(artifact)


def test_artifact_validation_requires_ocr_engine_evidence() -> None:
    artifact = _artifact(engines=())
    with pytest.raises(ValueError, match="engine"):
        validate_artifact_for_handoff(artifact)


def test_artifact_validation_requires_ocr_model_evidence() -> None:
    artifact = _artifact(ocr_document=_ocr(model_id="traineddata-ukr"), models=())
    with pytest.raises(ValueError, match="model"):
        validate_artifact_for_handoff(artifact)


def test_artifact_validation_rejects_ocr_model_engine_mismatch() -> None:
    artifact = _artifact(
        ocr_document=_ocr(model_id="traineddata-ukr"),
        models=(_model(engine_id="different-engine"),),
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_artifact_for_handoff(artifact)


def test_artifact_validation_requires_linear_revision_chain() -> None:
    first = _revision(0)
    second = _revision(1, parent="wrong")
    artifact = _artifact(revisions=(first, second))
    with pytest.raises(ValueError, match="immediately previous"):
        validate_artifact_for_handoff(artifact)


def test_handoff_preserves_timing_page_loci_and_privacy_without_engine_leakage() -> None:
    artifact = _artifact()
    handoff = build_corpus_media_handoff(artifact)
    assert handoff.schema_version == 1
    assert handoff.privacy == "private"
    assert handoff.content_sha256 == _SHA_B
    assert [block.source_kind for block in handoff.blocks] == [
        MediaTextSourceKind.TRANSCRIPT,
        MediaTextSourceKind.TRANSCRIPT,
        MediaTextSourceKind.OCR,
        MediaTextSourceKind.OCR,
    ]
    assert handoff.blocks[0].start_ms == 1000
    assert handoff.blocks[2].page_number == 1
    serialized = handoff.model_dump_json()
    assert "tesseract" not in serialized
    assert "traineddata" not in serialized
    assert "private_note" not in serialized


def test_handoff_skips_empty_source_text() -> None:
    transcript = Transcript(
        transcript_id="t-empty",
        version_id="version-1",
        method=TranscriptMethod.OFFLINE_ASR,
        segments=(Segment(segment_id="empty", start_ms=0, end_ms=10, text="   "),),
    )
    ocr = OCRDocument(
        document_id="o-empty",
        version_id="version-1",
        engine_id="tesseract",
        pages=(OCRPage(page_number=1, text="  ", source_sha256=_SHA_B),),
    )
    artifact = _artifact(transcript=transcript, ocr_document=ocr)
    with pytest.raises(ValidationError, match="requires text blocks"):
        build_corpus_media_handoff(artifact)


def test_handoff_exports_only_latest_accepted_revision() -> None:
    first = _revision(0, accepted=True)
    second = _revision(1, accepted=False, parent=first.revision_id)
    third = _revision(2, accepted=True, parent=second.revision_id)
    handoff = build_corpus_media_handoff(_artifact(revisions=(first, second, third)))
    assert handoff.accepted_revision is not None
    assert handoff.accepted_revision.revision_id == third.revision_id
    assert handoff.accepted_revision.ordinal == 2
    assert second.text not in handoff.accepted_revision.text


def test_handoff_exposes_provenance_hashes_but_not_private_details() -> None:
    handoff = build_corpus_media_handoff(_artifact())
    assert handoff.provenance[0].sequence == 0
    assert handoff.provenance[0].event_type == "media.imported"
    assert handoff.provenance[0].output_sha256 == (_SHA_B,)
    assert "details" not in handoff.provenance[0].model_fields


def test_delivery_coordinator_hydrates_append_only_revisions(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _persist_base(repository)
    first = _revision(0)
    second = _revision(1, parent=first.revision_id)
    repository.append_revision(first)
    repository.append_revision(second)
    coordinator = MediaDeliveryCoordinator(repository)
    materialized = coordinator.materialize_artifact("artifact-1")
    assert [revision.revision_id for revision in materialized.revisions] == [
        "revision-0",
        "revision-1",
    ]
    handoff = coordinator.build_handoff("artifact-1")
    assert handoff.accepted_revision is not None
    assert handoff.accepted_revision.revision_id == "revision-1"


def test_delivery_coordinator_fails_closed_on_conflicting_revision_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    artifact = _artifact(revisions=(_revision(0),))
    _persist_base(repository, artifact)
    coordinator = MediaDeliveryCoordinator(repository)
    with pytest.raises(ValueError, match="conflicts"):
        coordinator.materialize_artifact("artifact-1")


def test_delivery_coordinator_resolves_only_original_or_document_ocr_assets(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    artifact = _artifact()
    _persist_base(repository, artifact)
    coordinator = MediaDeliveryCoordinator(repository)
    request = OCRInputRequestV1(
        request_id="request-1",
        source_id="source-1",
        version_id="version-1",
        asset_id="asset-1",
        reason=OCRRequestReason.TEXT_LAYER_MISSING,
    )
    assert coordinator.resolve_ocr_request(request).asset_id == "asset-1"

    other_repository = _repository(tmp_path / "other")
    other_artifact = _artifact(assets=(_asset(kind=AssetKind.AUDIO),))
    _persist_base(other_repository, other_artifact)
    other = MediaDeliveryCoordinator(other_repository)
    with pytest.raises(ValueError, match="original or document"):
        other.resolve_ocr_request(request)


def test_delivery_coordinator_rejects_unknown_ocr_asset(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _persist_base(repository)
    coordinator = MediaDeliveryCoordinator(repository)
    request = OCRInputRequestV1(
        request_id="request-1",
        source_id="source-1",
        version_id="version-1",
        asset_id="missing",
        reason=OCRRequestReason.TEXT_LAYER_INSUFFICIENT,
    )
    with pytest.raises(KeyError, match="Unknown media asset"):
        coordinator.resolve_ocr_request(request)


def test_accessible_presenter_exposes_timestamps_pages_evidence_and_errors() -> None:
    artifact = _artifact(models=(_model(),), ocr_document=_ocr(model_id="traineddata-ukr"))
    handoff = build_corpus_media_handoff(artifact)
    text = render_accessible_media_text(artifact, handoff, errors=("OCR page 3 failed",))
    assert "00:01.000–00:02.400" in text
    assert "Page 1" in text
    assert "Engine Tesseract 5.5.2; license Apache-2.0" in text
    assert "Model traineddata-ukr" in text
    assert "Error: OCR page 3 failed" in text
    assert "Provenance" in text


def test_accessible_presenter_has_explicit_empty_states() -> None:
    artifact = _artifact(transcript=None, ocr_document=None, revisions=(_revision(0),))
    handoff = build_corpus_media_handoff(artifact)
    text = render_accessible_media_text(artifact, handoff)
    assert "No timed or paged text blocks are available." in text
    assert "No model descriptors recorded." in text
    assert "No errors recorded." in text
