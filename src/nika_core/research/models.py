from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SourceKind(StrEnum):
    LOCAL_FILE = "local_file"
    HTTP = "http"


class IngestDisposition(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"


class ExtractionStatus(StrEnum):
    EXTRACTED = "extracted"
    OCR_NEEDED = "ocr_needed"
    EMPTY = "empty"
    FAILED = "failed"


class RefreshDisposition(StrEnum):
    CHANGED = "changed"
    NOT_MODIFIED = "not_modified"
    UNCHANGED = "unchanged"
    DYNAMIC_REQUIRED = "dynamic_required"
    REMOVED = "removed"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ResearchFetchFailureClass(StrEnum):
    NETWORK = "network"
    PRIVATE = "private"
    AUTH = "auth"
    UNSUPPORTED = "unsupported"
    POLICY = "policy"
    HTTP = "http"
    RESOURCE = "resource"


class FreshnessState(StrEnum):
    UNKNOWN = "unknown"
    CURRENT = "current"
    STALE = "stale"
    REMOVED = "removed"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResearchWorkspace:
    workspace_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str
    workspace_id: str
    kind: SourceKind
    locator: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    title: str
    text: str
    media_type: str
    status: ExtractionStatus = ExtractionStatus.EXTRACTED
    extractor: str = "nika-stdlib"
    extractor_version: str = "1"


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    document_id: str
    workspace_id: str
    title: str
    normalized_sha256: str
    text: str
    media_type: str


@dataclass(frozen=True, slots=True)
class BlobArtifact:
    artifact_id: str
    workspace_id: str
    raw_sha256: str
    byte_size: int
    storage_relpath: str


@dataclass(frozen=True, slots=True)
class ExtractionRecord:
    extraction_id: str
    artifact_id: str
    extractor: str
    extractor_version: str
    status: ExtractionStatus
    normalized_text_sha256: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    disposition: IngestDisposition
    document: CorpusDocument
    source_id: str
    origin_locator: str


@dataclass(frozen=True, slots=True)
class ArtifactIngestResult:
    artifact: BlobArtifact
    extraction: ExtractionRecord
    corpus: IngestResult | None


@dataclass(frozen=True, slots=True)
class FolderIngestFailure:
    locator: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class FolderIngestResult:
    imported: tuple[ArtifactIngestResult, ...]
    failures: tuple[FolderIngestFailure, ...]
    skipped_unsupported: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: str
    title: str
    snippet: str
    rank: float


@dataclass(frozen=True, slots=True)
class HttpSourceState:
    source_id: str
    workspace_id: str
    url: str
    final_url: str | None
    etag: str | None
    last_modified: str | None
    current_raw_sha256: str | None
    freshness: FreshnessState
    last_attempt_at: str | None
    last_success_at: str | None
    last_status_code: int | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class RefreshResult:
    source_id: str
    disposition: RefreshDisposition
    attempts: int
    status_code: int | None = None
    snapshot_id: str | None = None
    document_id: str | None = None
    error_code: str | None = None
    message: str = ""
    failure_class: ResearchFetchFailureClass | None = None


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    source_id: str
    source_kind: SourceKind
    locator: str
    observed_at: str
    freshness: FreshnessState | None = None


@dataclass(frozen=True, slots=True)
class ResearchResultItem:
    ordinal: int
    document_id: str
    title: str
    snippet: str
    rank: float
    why_matched: str
    evidence: tuple[ResearchEvidence, ...]


@dataclass(frozen=True, slots=True)
class ResearchResultSet:
    result_set_id: str
    workspace_id: str
    query: str
    items: tuple[ResearchResultItem, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class RefreshJobSummary:
    task_id: str
    state: str
    processed: int
    total: int
    changed: int
    unchanged: int
    failed: int
