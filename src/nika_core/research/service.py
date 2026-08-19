from __future__ import annotations

import hashlib
from pathlib import Path

from nika_core.research.blobs import BlobStoreError, ContentAddressedBlobStore
from nika_core.research.documents import (
    DocumentIngestionError,
    DocumentLimits,
    document_extractor_identity,
    extract_document_file,
)
from nika_core.research.local import (
    LocalIngestionError,
    UnsupportedLocalFormatError,
    extract_local_file,
    is_document_format,
    local_media_type,
    resolve_local_file,
    resolve_local_folder,
)
from nika_core.research.models import (
    ArtifactIngestResult,
    ExtractionStatus,
    FolderIngestFailure,
    FolderIngestResult,
    IngestResult,
    SourceKind,
    SourceSpec,
)
from nika_core.research.normalize import normalize_text
from nika_core.research.repository import ResearchRepository


def _stable_folder_source_id(workspace_id: str, path: Path) -> str:
    digest = hashlib.sha256(f"{workspace_id}\0{path}".encode()).hexdigest()
    return f"local-{digest[:24]}"


class LocalCorpusService:
    """Deterministic local-source vertical with optional durable raw artifacts."""

    def __init__(self, repository: ResearchRepository, *, allowed_root: Path | str) -> None:
        self._repository = repository
        self._allowed_root = Path(allowed_root)

    def ingest(self, source: SourceSpec, *, max_bytes: int = 16 * 1024 * 1024) -> IngestResult:
        extracted = extract_local_file(
            source.locator,
            allowed_root=self._allowed_root,
            max_bytes=max_bytes,
        )
        self._repository.upsert_source(source)
        return self._repository.ingest_document(source, extracted)

    def ingest_artifact(
        self,
        source: SourceSpec,
        *,
        blob_store: ContentAddressedBlobStore,
        max_bytes: int = 64 * 1024 * 1024,
        document_limits: DocumentLimits | None = None,
    ) -> ArtifactIngestResult:
        if source.kind is not SourceKind.LOCAL_FILE:
            raise ValueError("artifact ingestion requires a local_file source")
        candidate = resolve_local_file(
            source.locator,
            allowed_root=self._allowed_root,
            max_bytes=max_bytes,
        )
        media_type = local_media_type(candidate)
        self._repository.upsert_source(source)
        artifact = blob_store.put_file(
            source.workspace_id,
            candidate,
            max_bytes=max_bytes,
        )
        self._repository.record_artifact(
            source,
            artifact,
            media_type=media_type,
            original_name=candidate.name,
        )

        if is_document_format(candidate):
            extractor, extractor_version = document_extractor_identity(candidate)
        else:
            extractor, extractor_version = "nika-stdlib", "1"

        try:
            if is_document_format(candidate):
                extracted = extract_document_file(candidate, limits=document_limits)
            else:
                extracted = extract_local_file(
                    candidate,
                    allowed_root=self._allowed_root,
                    max_bytes=max_bytes,
                )
        except Exception as exc:
            self._repository.record_extraction(
                artifact_id=artifact.artifact_id,
                extractor=extractor,
                extractor_version=extractor_version,
                status=ExtractionStatus.FAILED,
                normalized_text_sha256=None,
                detail=f"{type(exc).__name__}: {exc}"[:1000],
            )
            raise

        normalized = normalize_text(extracted.text)
        status = extracted.status
        if status is ExtractionStatus.EXTRACTED and not normalized:
            status = ExtractionStatus.EMPTY
        text_sha256 = hashlib.sha256(normalized.encode()).hexdigest() if normalized else None
        extraction = self._repository.record_extraction(
            artifact_id=artifact.artifact_id,
            extractor=extracted.extractor,
            extractor_version=extracted.extractor_version,
            status=status,
            normalized_text_sha256=text_sha256,
        )
        if status is not ExtractionStatus.EXTRACTED:
            return ArtifactIngestResult(artifact=artifact, extraction=extraction, corpus=None)

        corpus = self._repository.ingest_document(source, extracted)
        self._repository.link_document_artifact(
            document_id=corpus.document.document_id,
            artifact_id=artifact.artifact_id,
            extraction_id=extraction.extraction_id,
        )
        return ArtifactIngestResult(artifact=artifact, extraction=extraction, corpus=corpus)

    def ingest_folder(
        self,
        workspace_id: str,
        folder: Path | str,
        *,
        blob_store: ContentAddressedBlobStore,
        recursive: bool = True,
        max_files: int = 1000,
        max_bytes_per_file: int = 64 * 1024 * 1024,
        document_limits: DocumentLimits | None = None,
    ) -> FolderIngestResult:
        if not workspace_id.strip():
            raise ValueError("workspace_id is required")
        if max_files < 1:
            raise ValueError("max_files must be positive")
        root = resolve_local_folder(folder, allowed_root=self._allowed_root)
        iterator = root.rglob("*") if recursive else root.glob("*")
        candidates = sorted(
            (path for path in iterator if path.is_file()),
            key=lambda path: (
                path.relative_to(root).as_posix().casefold(),
                path.relative_to(root).as_posix(),
            ),
        )

        imported: list[ArtifactIngestResult] = []
        failures: list[FolderIngestFailure] = []
        skipped_unsupported = 0
        supported = 0
        for candidate in candidates:
            try:
                local_media_type(candidate)
            except UnsupportedLocalFormatError:
                skipped_unsupported += 1
                continue
            supported += 1
            if supported > max_files:
                raise ValueError(f"folder contains more than {max_files} supported files")

            resolved = candidate.resolve()
            source = SourceSpec(
                source_id=_stable_folder_source_id(workspace_id, resolved),
                workspace_id=workspace_id,
                kind=SourceKind.LOCAL_FILE,
                locator=str(resolved),
            )
            try:
                imported.append(
                    self.ingest_artifact(
                        source,
                        blob_store=blob_store,
                        max_bytes=max_bytes_per_file,
                        document_limits=document_limits,
                    )
                )
            except (BlobStoreError, DocumentIngestionError, LocalIngestionError) as exc:
                failures.append(
                    FolderIngestFailure(
                        locator=str(candidate),
                        error_type=type(exc).__name__,
                        message=str(exc)[:1000],
                    )
                )

        return FolderIngestResult(
            imported=tuple(imported),
            failures=tuple(failures),
            skipped_unsupported=skipped_unsupported,
        )