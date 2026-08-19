from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SourceKind(StrEnum):
    LOCAL_FILE = "local_file"


class IngestDisposition(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"


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


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    document_id: str
    workspace_id: str
    title: str
    normalized_sha256: str
    text: str
    media_type: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    disposition: IngestDisposition
    document: CorpusDocument
    source_id: str
    origin_locator: str


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: str
    title: str
    snippet: str
    rank: float
