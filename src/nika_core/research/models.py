from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SourceKind(StrEnum):
    LOCAL_FILE = "local_file"


class IngestDisposition(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"


class ExtractionStatus(StrEnum):
    EXTRACTED = "extracted"
    OCR_NEEDED = "ocr_needed"
    EMPTY = "empty"
    FAILED = "failed"


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
