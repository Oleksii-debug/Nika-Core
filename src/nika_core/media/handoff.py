from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nika_core.media.contracts import StructuredMediaArtifact, TextRevision


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OCRRequestReason(StrEnum):
    TEXT_LAYER_MISSING = "text_layer_missing"
    TEXT_LAYER_INSUFFICIENT = "text_layer_insufficient"


class MediaTextSourceKind(StrEnum):
    TRANSCRIPT = "transcript"
    OCR = "ocr"


class OCRInputRequestV1(_FrozenModel):
    """Secret-free request from a document/corpus lane into DEV05 OCR.

    The request carries only stable Nika identities. It deliberately cannot carry a URL,
    cookie, token, browser profile, arbitrary local path, or engine-specific option.
    DEV05 resolves the referenced immutable media asset through its own repository boundary.
    """

    schema_version: int = Field(default=1, ge=1, le=1)
    request_id: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(min_length=1, max_length=160)
    asset_id: str = Field(min_length=1, max_length=160)
    reason: OCRRequestReason
    page_numbers: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_pages(self) -> OCRInputRequestV1:
        if any(page < 1 for page in self.page_numbers):
            raise ValueError("OCR page numbers must be >= 1")
        if tuple(sorted(set(self.page_numbers))) != self.page_numbers:
            raise ValueError("OCR page numbers must be unique and sorted")
        return self


class CorpusMediaTextBlockV1(_FrozenModel):
    block_id: str = Field(min_length=1, max_length=240)
    source_kind: MediaTextSourceKind
    text: str = Field(min_length=1)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    page_number: int | None = Field(default=None, ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_locus(self) -> CorpusMediaTextBlockV1:
        has_time = self.start_ms is not None or self.end_ms is not None
        has_page = self.page_number is not None
        if has_time == has_page:
            raise ValueError("media handoff block must have exactly one timing or page locus")
        if has_time:
            if self.start_ms is None or self.end_ms is None:
                raise ValueError("timed media handoff block requires start_ms and end_ms")
            if self.end_ms < self.start_ms:
                raise ValueError("timed media handoff block end_ms must be >= start_ms")
        return self


class CorpusMediaRevisionV1(_FrozenModel):
    revision_id: str
    ordinal: int = Field(ge=0)
    text: str
    reason: str


class CorpusMediaProvenanceRefV1(_FrozenModel):
    sequence: int = Field(ge=0)
    event_type: str
    input_sha256: tuple[str, ...] = ()
    output_sha256: tuple[str, ...] = ()


class CorpusMediaHandoffV1(_FrozenModel):
    """Stable DEV05 -> DEV01 text handoff with no ASR/OCR engine leakage."""

    schema_version: int = Field(default=1, ge=1, le=1)
    artifact_id: str
    source_id: str
    version_id: str
    privacy: str = Field(pattern="^(public|private|sensitive)$")
    content_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    blocks: tuple[CorpusMediaTextBlockV1, ...]
    accepted_revision: CorpusMediaRevisionV1 | None = None
    provenance: tuple[CorpusMediaProvenanceRefV1, ...] = ()

    @model_validator(mode="after")
    def validate_payload(self) -> CorpusMediaHandoffV1:
        if not self.blocks and self.accepted_revision is None:
            raise ValueError("media handoff requires text blocks or an accepted revision")
        return self


def _latest_accepted_revision(revisions: tuple[TextRevision, ...]) -> TextRevision | None:
    accepted = [revision for revision in revisions if revision.accepted]
    if not accepted:
        return None
    return max(accepted, key=lambda revision: revision.ordinal)


def validate_artifact_for_handoff(artifact: StructuredMediaArtifact) -> None:
    if any(asset.version_id != artifact.version_id for asset in artifact.assets):
        raise ValueError("all media assets must belong to the artifact version")
    if artifact.transcript is not None and artifact.transcript.version_id != artifact.version_id:
        raise ValueError("transcript must belong to the artifact version")
    if artifact.ocr_document is not None and artifact.ocr_document.version_id != artifact.version_id:
        raise ValueError("OCR document must belong to the artifact version")

    engine_by_id = {}
    for engine in artifact.engines:
        if engine.engine_id in engine_by_id:
            raise ValueError(f"duplicate media engine identity: {engine.engine_id}")
        engine_by_id[engine.engine_id] = engine

    model_by_id = {}
    for model in artifact.models:
        if model.model_id in model_by_id:
            raise ValueError(f"duplicate media model identity: {model.model_id}")
        model_by_id[model.model_id] = model

    if artifact.ocr_document is not None:
        if artifact.ocr_document.engine_id not in engine_by_id:
            raise ValueError("OCR document references an engine missing from artifact evidence")
        if artifact.ocr_document.model_id is not None:
            model = model_by_id.get(artifact.ocr_document.model_id)
            if model is None:
                raise ValueError("OCR document references a model missing from artifact evidence")
            if model.engine_id != artifact.ocr_document.engine_id:
                raise ValueError("OCR model and OCR engine identity mismatch")

    for model in artifact.models:
        if model.engine_id not in engine_by_id:
            raise ValueError(
                f"media model {model.model_id} references an engine missing from artifact evidence"
            )

    previous_revision_id: str | None = None
    for ordinal, revision in enumerate(artifact.revisions):
        if revision.artifact_id != artifact.artifact_id:
            raise ValueError("text revision belongs to a different artifact")
        if revision.ordinal != ordinal:
            raise ValueError("text revision ordinals must be contiguous")
        if ordinal == 0:
            if revision.parent_revision_id is not None:
                raise ValueError("initial text revision must not have a parent")
        elif revision.parent_revision_id != previous_revision_id:
            raise ValueError("text revision parent must reference the immediately previous revision")
        previous_revision_id = revision.revision_id


def build_corpus_media_handoff(artifact: StructuredMediaArtifact) -> CorpusMediaHandoffV1:
    validate_artifact_for_handoff(artifact)

    blocks: list[CorpusMediaTextBlockV1] = []
    if artifact.transcript is not None:
        for segment in artifact.transcript.segments:
            text = segment.text.strip()
            if not text:
                continue
            blocks.append(
                CorpusMediaTextBlockV1(
                    block_id=f"transcript:{segment.segment_id}",
                    source_kind=MediaTextSourceKind.TRANSCRIPT,
                    text=text,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    confidence=segment.confidence,
                )
            )

    if artifact.ocr_document is not None:
        for page in artifact.ocr_document.pages:
            text = page.text.strip()
            if not text:
                continue
            blocks.append(
                CorpusMediaTextBlockV1(
                    block_id=f"ocr:page:{page.page_number}",
                    source_kind=MediaTextSourceKind.OCR,
                    text=text,
                    page_number=page.page_number,
                    confidence=page.confidence,
                )
            )

    revision = _latest_accepted_revision(artifact.revisions)
    handoff_revision = None
    if revision is not None:
        handoff_revision = CorpusMediaRevisionV1(
            revision_id=revision.revision_id,
            ordinal=revision.ordinal,
            text=revision.text,
            reason=revision.reason,
        )

    provenance = tuple(
        CorpusMediaProvenanceRefV1(
            sequence=event.sequence,
            event_type=event.event_type,
            input_sha256=event.input_sha256,
            output_sha256=event.output_sha256,
        )
        for event in artifact.provenance.events
    )

    return CorpusMediaHandoffV1(
        artifact_id=artifact.artifact_id,
        source_id=artifact.source.source_id,
        version_id=artifact.version_id,
        privacy=artifact.source.privacy,
        content_sha256=artifact.version.content_sha256,
        blocks=tuple(blocks),
        accepted_revision=handoff_revision,
        provenance=provenance,
    )
