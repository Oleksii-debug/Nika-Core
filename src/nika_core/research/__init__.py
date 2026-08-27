from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ArtifactIngestResult": ("nika_core.research.models", "ArtifactIngestResult"),
    "BlobArtifact": ("nika_core.research.models", "BlobArtifact"),
    "BlobStoreError": ("nika_core.research.blobs", "BlobStoreError"),
    "ChunkPolicy": ("nika_core.research.chunking", "ChunkPolicy"),
    "ContentAddressedBlobStore": (
        "nika_core.research.blobs",
        "ContentAddressedBlobStore",
    ),
    "CorpusDocument": ("nika_core.research.models", "CorpusDocument"),
    "DeterministicResearchQueryService": (
        "nika_core.research.query",
        "DeterministicResearchQueryService",
    ),
    "DocumentIngestionError": (
        "nika_core.research.documents",
        "DocumentIngestionError",
    ),
    "DocumentLimits": ("nika_core.research.documents", "DocumentLimits"),
    "DocumentSecurityError": (
        "nika_core.research.documents",
        "DocumentSecurityError",
    ),
    "DocumentTooLargeError": (
        "nika_core.research.documents",
        "DocumentTooLargeError",
    ),
    "ExtractedDocument": ("nika_core.research.models", "ExtractedDocument"),
    "ExtractionRecord": ("nika_core.research.models", "ExtractionRecord"),
    "ExtractionStatus": ("nika_core.research.models", "ExtractionStatus"),
    "FolderIngestFailure": ("nika_core.research.models", "FolderIngestFailure"),
    "FolderIngestResult": ("nika_core.research.models", "FolderIngestResult"),
    "FreshnessState": ("nika_core.research.models", "FreshnessState"),
    "HttpFetchPolicy": ("nika_core.research.http", "HttpFetchPolicy"),
    "HttpFetchResult": ("nika_core.research.http", "HttpFetchResult"),
    "HttpResearchService": (
        "nika_core.research.web_service",
        "HttpResearchService",
    ),
    "HttpSourceState": ("nika_core.research.models", "HttpSourceState"),
    "HttpValidators": ("nika_core.research.http", "HttpValidators"),
    "HttpxResearchFetcher": ("nika_core.research.http", "HttpxResearchFetcher"),
    "IngestDisposition": ("nika_core.research.models", "IngestDisposition"),
    "IngestResult": ("nika_core.research.models", "IngestResult"),
    "LocalCorpusService": ("nika_core.research.service", "LocalCorpusService"),
    "LocalFileTooLargeError": (
        "nika_core.research.local",
        "LocalFileTooLargeError",
    ),
    "LocalIngestionError": ("nika_core.research.local", "LocalIngestionError"),
    "LocalPathPolicyError": ("nika_core.research.local", "LocalPathPolicyError"),
    "NetworkPolicyError": ("nika_core.research.http", "NetworkPolicyError"),
    "NetworkResearchRepository": (
        "nika_core.research.network_repository",
        "NetworkResearchRepository",
    ),
    "PaginatedResearchRefreshService": (
        "nika_core.research.pagination_jobs",
        "PaginatedResearchRefreshService",
    ),
    "PaginationDiscovery": (
        "nika_core.research.pagination",
        "PaginationDiscovery",
    ),
    "PaginationPolicy": ("nika_core.research.pagination", "PaginationPolicy"),
    "RefreshDisposition": ("nika_core.research.models", "RefreshDisposition"),
    "RefreshJobSummary": ("nika_core.research.models", "RefreshJobSummary"),
    "RefreshResult": ("nika_core.research.models", "RefreshResult"),
    "ResearchEvidence": ("nika_core.research.models", "ResearchEvidence"),
    "ResearchFetchFailureClass": (
        "nika_core.research.models",
        "ResearchFetchFailureClass",
    ),
    "ResearchProfile": ("nika_core.research.profiles", "ResearchProfile"),
    "ResearchProfileExecution": (
        "nika_core.research.profiles",
        "ResearchProfileExecution",
    ),
    "ResearchProfileRepository": (
        "nika_core.research.profiles",
        "ResearchProfileRepository",
    ),
    "ResearchProfileRunService": (
        "nika_core.research.profile_jobs",
        "ResearchProfileRunService",
    ),
    "ResearchProfileRunSummary": (
        "nika_core.research.profile_jobs",
        "ResearchProfileRunSummary",
    ),
    "ResearchProfileService": (
        "nika_core.research.profiles",
        "ResearchProfileService",
    ),
    "ResearchQueryExecution": ("nika_core.research.query", "ResearchQueryExecution"),
    "ResearchQuerySpec": ("nika_core.research.query", "ResearchQuerySpec"),
    "ResearchRefreshService": ("nika_core.research.jobs", "ResearchRefreshService"),
    "ResearchRepository": ("nika_core.research.repository", "ResearchRepository"),
    "ResearchResultItem": ("nika_core.research.models", "ResearchResultItem"),
    "ResearchResultService": ("nika_core.research.results", "ResearchResultService"),
    "ResearchResultSet": ("nika_core.research.models", "ResearchResultSet"),
    "ResearchSearchFilters": ("nika_core.research.query", "ResearchSearchFilters"),
    "ResearchSourceIdentityError": (
        "nika_core.research.source_identity",
        "ResearchSourceIdentityError",
    ),
    "ResearchSourceRef": ("nika_core.research.profiles", "ResearchSourceRef"),
    "ResearchSourceSet": ("nika_core.research.profiles", "ResearchSourceSet"),
    "ResearchWorkspace": ("nika_core.research.models", "ResearchWorkspace"),
    "ResponseTooLargeError": (
        "nika_core.research.http",
        "ResponseTooLargeError",
    ),
    "SearchHit": ("nika_core.research.models", "SearchHit"),
    "SearchMode": ("nika_core.research.query", "SearchMode"),
    "SourceKind": ("nika_core.research.models", "SourceKind"),
    "SourceSpec": ("nika_core.research.models", "SourceSpec"),
    "UnsupportedLocalFormatError": (
        "nika_core.research.local",
        "UnsupportedLocalFormatError",
    ),
    "canonical_http_locator": (
        "nika_core.research.source_identity",
        "canonical_http_locator",
    ),
    "chunk_text": ("nika_core.research.chunking", "chunk_text"),
    "discover_html_pagination": (
        "nika_core.research.pagination",
        "discover_html_pagination",
    ),
    "discover_json_pagination": (
        "nika_core.research.pagination",
        "discover_json_pagination",
    ),
    "extend_pagination_frontier": (
        "nika_core.research.pagination",
        "extend_pagination_frontier",
    ),
    "extract_document_file": (
        "nika_core.research.documents",
        "extract_document_file",
    ),
    "extract_local_file": ("nika_core.research.local", "extract_local_file"),
    "normalize_text": ("nika_core.research.normalize", "normalize_text"),
    "preflight_office_archive": (
        "nika_core.research.documents",
        "preflight_office_archive",
    ),
}

__all__ = [
    "ArtifactIngestResult",
    "BlobArtifact",
    "BlobStoreError",
    "ChunkPolicy",
    "ContentAddressedBlobStore",
    "CorpusDocument",
    "DeterministicResearchQueryService",
    "DocumentIngestionError",
    "DocumentLimits",
    "DocumentSecurityError",
    "DocumentTooLargeError",
    "ExtractedDocument",
    "ExtractionRecord",
    "ExtractionStatus",
    "FolderIngestFailure",
    "FolderIngestResult",
    "FreshnessState",
    "HttpFetchPolicy",
    "HttpFetchResult",
    "HttpResearchService",
    "HttpSourceState",
    "HttpValidators",
    "HttpxResearchFetcher",
    "IngestDisposition",
    "IngestResult",
    "LocalCorpusService",
    "LocalFileTooLargeError",
    "LocalIngestionError",
    "LocalPathPolicyError",
    "NetworkPolicyError",
    "NetworkResearchRepository",
    "PaginatedResearchRefreshService",
    "PaginationDiscovery",
    "PaginationPolicy",
    "RefreshDisposition",
    "RefreshJobSummary",
    "RefreshResult",
    "ResearchEvidence",
    "ResearchFetchFailureClass",
    "ResearchProfile",
    "ResearchProfileExecution",
    "ResearchProfileRepository",
    "ResearchProfileRunService",
    "ResearchProfileRunSummary",
    "ResearchProfileService",
    "ResearchQueryExecution",
    "ResearchQuerySpec",
    "ResearchRefreshService",
    "ResearchRepository",
    "ResearchResultItem",
    "ResearchResultService",
    "ResearchResultSet",
    "ResearchSearchFilters",
    "ResearchSourceIdentityError",
    "ResearchSourceRef",
    "ResearchSourceSet",
    "ResearchWorkspace",
    "ResponseTooLargeError",
    "SearchHit",
    "SearchMode",
    "SourceKind",
    "SourceSpec",
    "UnsupportedLocalFormatError",
    "canonical_http_locator",
    "chunk_text",
    "discover_html_pagination",
    "discover_json_pagination",
    "extend_pagination_frontier",
    "extract_document_file",
    "extract_local_file",
    "normalize_text",
    "preflight_office_archive",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
