from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
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


class CorpusMediaAssetEvidenceV2(_FrozenModel):
    asset_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)
    immutable_original: bool


class CorpusMediaTranscriptEvidenceV2(_FrozenModel):
    transcript_id: str = Field(min_length=1, max_length=160)
    method: str = Field(min_length=1, max_length=80)
    language: str | None = Field(default=None, max_length=80)
    source_track_id: str | None = Field(default=None, max_length=160)
    created_at: datetime
    transcript_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaOCREvidenceV2(_FrozenModel):
    document_id: str = Field(min_length=1, max_length=160)
    engine_id: str = Field(min_length=1, max_length=160)
    model_id: str | None = Field(default=None, max_length=160)
    document_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaEngineEvidenceV2(_FrozenModel):
    engine_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=240)
    version: str = Field(min_length=1, max_length=160)
    license_id: str = Field(min_length=1, max_length=240)
    executable_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    descriptor_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaModelEvidenceV2(_FrozenModel):
    model_id: str = Field(min_length=1, max_length=160)
    engine_id: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    descriptor_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaRevisionV2(_FrozenModel):
    revision_id: str = Field(min_length=1, max_length=160)
    parent_revision_id: str | None = Field(default=None, max_length=160)
    ordinal: int = Field(ge=0)
    text: str
    reason: str = Field(min_length=1, max_length=500)
    created_at: datetime
    revision_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaProvenanceRefV2(_FrozenModel):
    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=120)
    input_sha256: tuple[str, ...] = ()
    output_sha256: tuple[str, ...] = ()
    created_at: datetime
    event_sha256: str = Field(pattern="^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digests(self) -> CorpusMediaProvenanceRefV2:
        for digest in (*self.input_sha256, *self.output_sha256):
            if not _is_sha256(digest):
                raise ValueError("media provenance input/output hashes must be SHA-256")
        return self


class CorpusMediaHandoffV2(_FrozenModel):
    """Strict restart-safe media provenance envelope for Corpus ingestion.

    This schema intentionally omits source locators/auth references, local asset paths,
    engine/model reference URLs, build internals and provenance details. Their validated
    source records remain owned by the media repository; safe immutable identity evidence
    and fingerprints cross the Corpus boundary instead.

    `handoff_sha256` is a deterministic corruption/restart checksum, not an authority
    signature. `artifact_provenance_sha256` is the stable comparison key that Corpus-side
    durable integration can compare with the trusted media record before accepting data.
    """

    schema_version: int = Field(default=2, ge=2, le=2)
    artifact_id: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=160)
    source_kind: str = Field(min_length=1, max_length=80)
    source_created_at: datetime
    version_id: str = Field(min_length=1, max_length=160)
    version_observed_at: datetime
    privacy: str = Field(pattern="^(public|private|sensitive)$")
    metadata_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    content_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    assets: tuple[CorpusMediaAssetEvidenceV2, ...] = ()
    blocks: tuple[CorpusMediaTextBlockV1, ...]
    transcript: CorpusMediaTranscriptEvidenceV2 | None = None
    ocr: CorpusMediaOCREvidenceV2 | None = None
    accepted_revision: CorpusMediaRevisionV2 | None = None
    engines: tuple[CorpusMediaEngineEvidenceV2, ...] = ()
    models: tuple[CorpusMediaModelEvidenceV2, ...] = ()
    provenance: tuple[CorpusMediaProvenanceRefV2, ...] = ()
    artifact_provenance_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    handoff_sha256: str = Field(pattern="^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> CorpusMediaHandoffV2:
        if not self.blocks and self.accepted_revision is None:
            raise ValueError("media handoff requires text blocks or an accepted revision")

        asset_ids = tuple(item.asset_id for item in self.assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("media handoff asset identities must be unique")
        if asset_ids != tuple(sorted(asset_ids)):
            raise ValueError("media handoff asset evidence must use canonical identity order")

        engine_ids = tuple(item.engine_id for item in self.engines)
        if len(engine_ids) != len(set(engine_ids)):
            raise ValueError("media handoff engine identities must be unique")
        if engine_ids != tuple(sorted(engine_ids)):
            raise ValueError("media handoff engine evidence must use canonical identity order")
        engine_id_set = set(engine_ids)

        model_ids = tuple(item.model_id for item in self.models)
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("media handoff model identities must be unique")
        if model_ids != tuple(sorted(model_ids)):
            raise ValueError("media handoff model evidence must use canonical identity order")
        model_by_id = {item.model_id: item for item in self.models}
        for model in self.models:
            if model.engine_id not in engine_id_set:
                raise ValueError("media handoff model references missing engine evidence")

        if self.ocr is not None:
            if self.ocr.engine_id not in engine_id_set:
                raise ValueError("media handoff OCR evidence references missing engine evidence")
            if self.ocr.model_id is not None:
                model = model_by_id.get(self.ocr.model_id)
                if model is None:
                    raise ValueError("media handoff OCR evidence references missing model evidence")
                if model.engine_id != self.ocr.engine_id:
                    raise ValueError("media handoff OCR model/engine identity mismatch")

        sequences = tuple(item.sequence for item in self.provenance)
        if sequences != tuple(range(len(self.provenance))):
            raise ValueError("media handoff provenance must be contiguous from zero")

        expected = _handoff_v2_sha256(self)
        if not hmac.compare_digest(self.handoff_sha256, expected):
            raise ValueError("media handoff checksum mismatch")
        return self


def _latest_accepted_revision(revisions: tuple[TextRevision, ...]) -> TextRevision | None:
    accepted = [revision for revision in revisions if revision.accepted]
    if not accepted:
        return None
    return max(accepted, key=lambda revision: revision.ordinal)


def validate_artifact_for_handoff(artifact: StructuredMediaArtifact) -> None:
    if any(asset.version_id != artifact.version_id for asset in artifact.assets):
        raise ValueError("all media assets must belong to the artifact version")
    asset_ids = [asset.asset_id for asset in artifact.assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("media asset identities must be unique for Corpus handoff")
    if artifact.transcript is not None:
        if artifact.transcript.version_id != artifact.version_id:
            raise ValueError("transcript must belong to the artifact version")
        segment_ids = [segment.segment_id for segment in artifact.transcript.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("transcript segment identities must be unique for Corpus handoff")
    if artifact.ocr_document is not None:
        if artifact.ocr_document.version_id != artifact.version_id:
            raise ValueError("OCR document must belong to the artifact version")
        page_numbers = [page.page_number for page in artifact.ocr_document.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("OCR page identities must be unique for Corpus handoff")

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
    revision_ids: set[str] = set()
    for ordinal, revision in enumerate(artifact.revisions):
        if revision.revision_id in revision_ids:
            raise ValueError("text revision identities must be unique for Corpus handoff")
        revision_ids.add(revision.revision_id)
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


def build_corpus_media_handoff_v2(artifact: StructuredMediaArtifact) -> CorpusMediaHandoffV2:
    """Build the strict provenance envelope without exposing ephemeral or secret-bearing refs."""
    validate_artifact_for_handoff(artifact)
    text_handoff = build_corpus_media_handoff(artifact)

    assets = tuple(
        CorpusMediaAssetEvidenceV2(
            asset_id=asset.asset_id,
            kind=asset.kind.value,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            media_type=asset.media_type,
            immutable_original=asset.immutable_original,
        )
        for asset in sorted(artifact.assets, key=lambda item: item.asset_id)
    )

    transcript = None
    if artifact.transcript is not None:
        transcript = CorpusMediaTranscriptEvidenceV2(
            transcript_id=artifact.transcript.transcript_id,
            method=artifact.transcript.method.value,
            language=artifact.transcript.language,
            source_track_id=artifact.transcript.source_track_id,
            created_at=artifact.transcript.created_at,
            transcript_sha256=_canonical_sha256(artifact.transcript.model_dump(mode="json")),
        )

    ocr = None
    if artifact.ocr_document is not None:
        ocr = CorpusMediaOCREvidenceV2(
            document_id=artifact.ocr_document.document_id,
            engine_id=artifact.ocr_document.engine_id,
            model_id=artifact.ocr_document.model_id,
            document_sha256=_canonical_sha256(artifact.ocr_document.model_dump(mode="json")),
        )

    engines = tuple(
        CorpusMediaEngineEvidenceV2(
            engine_id=engine.engine_id,
            name=engine.name,
            version=engine.version,
            license_id=engine.license_id,
            executable_sha256=engine.executable_sha256,
            descriptor_sha256=_canonical_sha256(engine.model_dump(mode="json")),
        )
        for engine in sorted(artifact.engines, key=lambda item: item.engine_id)
    )
    models = tuple(
        CorpusMediaModelEvidenceV2(
            model_id=model.model_id,
            engine_id=model.engine_id,
            version=model.version,
            sha256=model.sha256,
            size_bytes=model.size_bytes,
            descriptor_sha256=_canonical_sha256(model.model_dump(mode="json")),
        )
        for model in sorted(artifact.models, key=lambda item: item.model_id)
    )

    revision = _latest_accepted_revision(artifact.revisions)
    accepted_revision = None
    if revision is not None:
        accepted_revision = CorpusMediaRevisionV2(
            revision_id=revision.revision_id,
            parent_revision_id=revision.parent_revision_id,
            ordinal=revision.ordinal,
            text=revision.text,
            reason=revision.reason,
            created_at=revision.created_at,
            revision_sha256=_canonical_sha256(revision.model_dump(mode="json")),
        )

    provenance = tuple(
        CorpusMediaProvenanceRefV2(
            sequence=event.sequence,
            event_type=event.event_type,
            actor=event.actor,
            input_sha256=event.input_sha256,
            output_sha256=event.output_sha256,
            created_at=event.created_at,
            event_sha256=_canonical_sha256(event.model_dump(mode="json")),
        )
        for event in artifact.provenance.events
    )

    payload = {
        "schema_version": 2,
        "artifact_id": artifact.artifact_id,
        "source_id": artifact.source.source_id,
        "source_kind": artifact.source.kind.value,
        "source_created_at": artifact.source.created_at,
        "version_id": artifact.version_id,
        "version_observed_at": artifact.version.observed_at,
        "privacy": artifact.source.privacy,
        "metadata_sha256": artifact.version.metadata_sha256,
        "content_sha256": artifact.version.content_sha256,
        "assets": assets,
        "blocks": text_handoff.blocks,
        "transcript": transcript,
        "ocr": ocr,
        "accepted_revision": accepted_revision,
        "engines": engines,
        "models": models,
        "provenance": provenance,
        "artifact_provenance_sha256": _artifact_provenance_sha256(artifact),
    }
    handoff_sha256 = _canonical_sha256(_jsonable(payload))
    return CorpusMediaHandoffV2(**payload, handoff_sha256=handoff_sha256)


def dump_corpus_media_handoff_v2(handoff: CorpusMediaHandoffV2) -> str:
    """Serialize V2 canonically for a durable restart checkpoint."""
    if not isinstance(handoff, CorpusMediaHandoffV2):
        raise TypeError("handoff must be CorpusMediaHandoffV2")
    return json.dumps(
        handoff.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_corpus_media_handoff_v2(payload: str) -> CorpusMediaHandoffV2:
    """Strictly restore a V2 checkpoint and revalidate its graph and checksum."""
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("media handoff checkpoint must be non-empty JSON text")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("media handoff checkpoint is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("media handoff checkpoint root must be an object")
    return CorpusMediaHandoffV2.model_validate(decoded)


def _artifact_provenance_sha256(artifact: StructuredMediaArtifact) -> str:
    """Fingerprint validated durable semantics while excluding secret/ephemeral raw refs."""
    payload = {
        "schema": "nika-media-corpus-artifact-provenance-v2",
        "artifact_id": artifact.artifact_id,
        "source": {
            "source_id": artifact.source.source_id,
            "kind": artifact.source.kind.value,
            "privacy": artifact.source.privacy,
            "created_at": artifact.source.created_at,
        },
        "version": {
            "version_id": artifact.version.version_id,
            "source_id": artifact.version.source_id,
            "observed_at": artifact.version.observed_at,
            "metadata_sha256": artifact.version.metadata_sha256,
            "content_sha256": artifact.version.content_sha256,
        },
        "assets": [
            {
                "asset_id": asset.asset_id,
                "version_id": asset.version_id,
                "kind": asset.kind.value,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
                "media_type": asset.media_type,
                "immutable_original": asset.immutable_original,
            }
            for asset in sorted(artifact.assets, key=lambda item: item.asset_id)
        ],
        "transcript_sha256": (
            _canonical_sha256(artifact.transcript.model_dump(mode="json"))
            if artifact.transcript is not None
            else None
        ),
        "ocr_sha256": (
            _canonical_sha256(artifact.ocr_document.model_dump(mode="json"))
            if artifact.ocr_document is not None
            else None
        ),
        "revision_sha256": [
            _canonical_sha256(revision.model_dump(mode="json")) for revision in artifact.revisions
        ],
        "engine_descriptor_sha256": [
            _canonical_sha256(engine.model_dump(mode="json"))
            for engine in sorted(artifact.engines, key=lambda item: item.engine_id)
        ],
        "model_descriptor_sha256": [
            _canonical_sha256(model.model_dump(mode="json"))
            for model in sorted(artifact.models, key=lambda item: item.model_id)
        ],
        "provenance_event_sha256": [
            _canonical_sha256(event.model_dump(mode="json")) for event in artifact.provenance.events
        ],
    }
    return _canonical_sha256(_jsonable(payload))


def _handoff_v2_sha256(handoff: CorpusMediaHandoffV2) -> str:
    return _canonical_sha256(
        handoff.model_dump(mode="json", exclude={"handoff_sha256"})
    )


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(payload: object) -> object:
    return json.loads(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else value.model_dump(mode="json"),
        )
    )


def _is_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()
