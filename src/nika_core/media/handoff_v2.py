from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nika_core.media.contracts import StructuredMediaArtifact, TextRevision
from nika_core.media.handoff import (
    CorpusMediaTextBlockV1,
    build_corpus_media_handoff,
    validate_artifact_for_handoff,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CorpusMediaAssetEvidenceV2(_FrozenModel):
    asset_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)
    immutable_original: bool


class CorpusMediaTranscriptEvidenceV2(_FrozenModel):
    transcript_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    language: str | None = None
    source_track_id: str | None = None
    created_at: datetime
    transcript_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaOCREvidenceV2(_FrozenModel):
    document_id: str = Field(min_length=1)
    engine_id: str = Field(min_length=1)
    model_id: str | None = None
    document_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaEngineEvidenceV2(_FrozenModel):
    engine_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    executable_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    descriptor_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaModelEvidenceV2(_FrozenModel):
    model_id: str = Field(min_length=1)
    engine_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    descriptor_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class CorpusMediaRevisionV2(_FrozenModel):
    revision_id: str = Field(min_length=1)
    parent_revision_id: str | None = None
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

    The serialized envelope excludes source locators/auth references, local asset paths,
    engine/model source locators and build internals, and provenance details. Safe immutable
    identities plus fingerprints cross the boundary instead.

    `handoff_sha256` is a deterministic corruption/restart checksum, not an authority
    signature. `artifact_provenance_sha256` is a comparison key for a trusted media record.
    """

    schema_version: int = Field(default=2, ge=2, le=2)
    artifact_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1, max_length=160)
    source_kind: str = Field(min_length=1)
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


def build_corpus_media_handoff_v2(artifact: StructuredMediaArtifact) -> CorpusMediaHandoffV2:
    """Build the strict provenance envelope after validating the complete media artifact."""
    _validate_v2_artifact(artifact)
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
    draft = CorpusMediaHandoffV2.model_construct(**payload, handoff_sha256="0" * 64)
    handoff_sha256 = _handoff_v2_sha256(draft)
    return CorpusMediaHandoffV2.model_validate({**payload, "handoff_sha256": handoff_sha256})


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
    """Strictly restore V2 and revalidate schema, evidence graph and checksum."""
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("media handoff checkpoint must be non-empty JSON text")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("media handoff checkpoint is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("media handoff checkpoint root must be an object")
    return CorpusMediaHandoffV2.model_validate(decoded)


def require_corpus_media_handoff_v2_matches_artifact(
    handoff: CorpusMediaHandoffV2,
    artifact: StructuredMediaArtifact,
) -> None:
    """Fail closed unless a handoff exactly matches a trusted durable media artifact.

    Corpus/workspace authorization remains outside this function: the caller must first
    resolve `handoff.source_id` through the trusted workspace-scoped Corpus/media boundary.
    This function then prevents a caller-recomputed checksum from becoming artifact authority.
    """
    if not isinstance(handoff, CorpusMediaHandoffV2):
        raise TypeError("handoff must be CorpusMediaHandoffV2")
    expected = build_corpus_media_handoff_v2(artifact)
    if not hmac.compare_digest(handoff.handoff_sha256, expected.handoff_sha256):
        raise ValueError("media handoff does not match trusted artifact")
    if not hmac.compare_digest(
        handoff.artifact_provenance_sha256,
        expected.artifact_provenance_sha256,
    ):
        raise ValueError("media handoff provenance does not match trusted artifact")
    if (
        handoff.artifact_id != expected.artifact_id
        or handoff.source_id != expected.source_id
        or handoff.version_id != expected.version_id
    ):
        raise ValueError("media handoff identity does not match trusted artifact")


def _validate_v2_artifact(artifact: StructuredMediaArtifact) -> None:
    if not isinstance(artifact, StructuredMediaArtifact):
        raise TypeError("media handoff requires StructuredMediaArtifact")
    if artifact.version.source_id != artifact.source.source_id:
        raise ValueError("artifact version/source identity mismatch")
    if artifact.version.version_id != artifact.version_id:
        raise ValueError("artifact version_id mismatch")
    validate_artifact_for_handoff(artifact)

    asset_ids = tuple(asset.asset_id for asset in artifact.assets)
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("media asset identities must be unique for Corpus handoff")

    revision_ids = tuple(revision.revision_id for revision in artifact.revisions)
    if len(revision_ids) != len(set(revision_ids)):
        raise ValueError("text revision identities must be unique for Corpus handoff")

    for event in artifact.provenance.events:
        for digest in (*event.input_sha256, *event.output_sha256):
            if not _is_sha256(digest):
                raise ValueError("media provenance input/output hashes must be SHA-256")


def _latest_accepted_revision(revisions: tuple[TextRevision, ...]) -> TextRevision | None:
    accepted = [revision for revision in revisions if revision.accepted]
    if not accepted:
        return None
    return max(accepted, key=lambda revision: revision.ordinal)


def _artifact_provenance_sha256(artifact: StructuredMediaArtifact) -> str:
    """Fingerprint durable semantics while excluding raw secret/ephemeral location fields."""
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
            "title": artifact.version.title,
            "duration_seconds": artifact.version.duration_seconds,
            "upstream_id": artifact.version.upstream_id,
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
    return _canonical_sha256(payload)


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
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        rendered = value.isoformat()
        return rendered.removesuffix("+00:00") + "Z" if rendered.endswith("+00:00") else rendered
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _is_sha256(value: str) -> bool:
    if len(value) != 64 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
