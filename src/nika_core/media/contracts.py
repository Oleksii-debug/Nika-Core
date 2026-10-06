from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MediaSourceKind(StrEnum):
    LOCAL_FILE = "local_file"
    REMOTE_MEDIA = "remote_media"


class AssetKind(StrEnum):
    ORIGINAL = "original"
    SUBTITLE = "subtitle"
    AUDIO = "audio"
    DOCUMENT = "document"
    DERIVED = "derived"


class TranscriptMethod(StrEnum):
    PLATFORM_SUBTITLE = "platform_subtitle"
    OFFLINE_ASR = "offline_asr"
    USER_SUPPLIED = "user_supplied"


class SubtitleKind(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    TRANSLATED = "translated"


class ProcessingState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ComponentState(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    DISABLED = "disabled"
    REQUIRES_APPROVAL = "requires_approval"
    INCOMPATIBLE = "incompatible"


class ResourceClass(StrEnum):
    LIGHT = "light"
    MEDIA_IO = "media_io"
    HEAVY_MODEL = "heavy_model"


class ProvenanceEvent(FrozenModel):
    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=120)
    input_sha256: tuple[str, ...] = ()
    output_sha256: tuple[str, ...] = ()
    details: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProvenanceChain(FrozenModel):
    events: tuple[ProvenanceEvent, ...] = ()

    @model_validator(mode="after")
    def validate_sequences(self) -> ProvenanceChain:
        expected = tuple(range(len(self.events)))
        actual = tuple(event.sequence for event in self.events)
        if actual != expected:
            raise ValueError("provenance event sequences must be contiguous from zero")
        return self


class MediaSource(FrozenModel):
    source_id: str = Field(min_length=1, max_length=160)
    kind: MediaSourceKind
    locator: str = Field(min_length=1, max_length=4096)
    privacy: str = Field(default="private", pattern="^(public|private|sensitive)$")
    auth_ref: str | None = Field(default=None, max_length=300)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("auth_ref")
    @classmethod
    def reject_auth_material(cls, value: str | None) -> str | None:
        if value is None:
            return value
        lowered = value.lower()
        forbidden = ("cookie=", "authorization:", "bearer ", "password=", "token=")
        if any(item in lowered for item in forbidden):
            raise ValueError("auth_ref must be an opaque reference, not credential material")
        return value


class MediaVersion(FrozenModel):
    version_id: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=160)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    content_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    title: str = Field(default="", max_length=1000)
    duration_seconds: float | None = Field(default=None, ge=0)
    upstream_id: str | None = Field(default=None, max_length=500)


class MediaAsset(FrozenModel):
    asset_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(min_length=1, max_length=160)
    kind: AssetKind
    relative_path: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", max_length=200)
    immutable_original: bool = False


class Probe(FrozenModel):
    asset_id: str
    container: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    bit_rate: int | None = Field(default=None, ge=0)
    streams: tuple[dict[str, Any], ...] = ()
    raw_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class SubtitleTrack(FrozenModel):
    track_id: str
    language: str = Field(min_length=1, max_length=40)
    kind: SubtitleKind
    name: str = Field(default="", max_length=300)
    url: str | None = Field(default=None, max_length=4096)
    format: str = Field(default="vtt", max_length=40)
    is_default: bool = False
    source_label: str = Field(default="", max_length=300)


class AudioTrack(FrozenModel):
    track_id: str
    language: str | None = Field(default=None, max_length=40)
    codec: str | None = Field(default=None, max_length=80)
    channels: int | None = Field(default=None, ge=1)
    sample_rate_hz: int | None = Field(default=None, ge=1)


class Segment(FrozenModel):
    segment_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_range(self) -> Segment:
        if self.end_ms < self.start_ms:
            raise ValueError("segment end_ms must be >= start_ms")
        return self


class Transcript(FrozenModel):
    transcript_id: str
    version_id: str
    method: TranscriptMethod
    language: str | None = None
    segments: tuple[Segment, ...]
    source_track_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_monotonic(self) -> Transcript:
        previous = -1
        for segment in self.segments:
            if segment.start_ms < previous:
                raise ValueError("transcript segments must be monotonic")
            previous = segment.start_ms
        return self


class OCRPage(FrozenModel):
    page_number: int = Field(ge=1)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class OCRDocument(FrozenModel):
    document_id: str
    version_id: str
    pages: tuple[OCRPage, ...]
    engine_id: str
    model_id: str | None = None


class TextRevision(FrozenModel):
    revision_id: str
    artifact_id: str
    parent_revision_id: str | None = None
    ordinal: int = Field(ge=0)
    text: str
    reason: str = Field(min_length=1, max_length=500)
    accepted: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProcessingJob(FrozenModel):
    job_id: str
    source_id: str
    version_id: str | None = None
    stage: str = Field(min_length=1, max_length=120)
    state: ProcessingState = ProcessingState.PENDING
    checkpoint_json: dict[str, Any] = Field(default_factory=dict)
    last_error_code: str | None = Field(default=None, max_length=120)
    last_error_message: str | None = Field(default=None, max_length=1000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EngineDescriptor(FrozenModel):
    engine_id: str
    name: str
    version: str
    license_id: str
    source_reference: str
    executable_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    build_configuration: str | None = None


class ModelDescriptor(FrozenModel):
    model_id: str
    engine_id: str
    version: str
    license_reference: str
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


class OptionalComponent(FrozenModel):
    component_id: str
    state: ComponentState
    installed_version: str | None = None
    path_hint: str | None = None
    message: str = ""


class MediaResourceClaim(FrozenModel):
    claim_id: str
    owner_id: str
    resource_class: ResourceClass
    max_concurrent: int = Field(default=1, ge=1)
    min_available_memory_bytes: int | None = Field(default=None, ge=1)
    mutually_exclusive_with: tuple[ResourceClass, ...] = ()


class StructuredMediaArtifact(FrozenModel):
    artifact_id: str
    version_id: str
    source: MediaSource
    version: MediaVersion
    assets: tuple[MediaAsset, ...] = ()
    transcript: Transcript | None = None
    ocr_document: OCRDocument | None = None
    revisions: tuple[TextRevision, ...] = ()
    engines: tuple[EngineDescriptor, ...] = ()
    models: tuple[ModelDescriptor, ...] = ()
    provenance: ProvenanceChain = Field(default_factory=ProvenanceChain)

    @model_validator(mode="after")
    def validate_identity(self) -> StructuredMediaArtifact:
        if self.version.source_id != self.source.source_id:
            raise ValueError("artifact version/source identity mismatch")
        if self.version.version_id != self.version_id:
            raise ValueError("artifact version_id mismatch")
        ordinals = [revision.ordinal for revision in self.revisions]
        if ordinals and ordinals != list(range(len(ordinals))):
            raise ValueError("text revision ordinals must be contiguous from zero")
        return self
