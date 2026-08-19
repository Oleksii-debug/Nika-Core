from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from nika_core.research.blobs import BlobStoreError, ContentAddressedBlobStore
from nika_core.research.documents import (
    DocumentIngestionError,
    DocumentLimits,
    document_extractor_identity,
    extract_document_file,
)
from nika_core.research.http import (
    HttpFetchPolicy,
    HttpFetchResult,
    HttpValidators,
)
from nika_core.research.local import (
    LocalIngestionError,
    extract_text_payload,
    is_document_media_type,
)
from nika_core.research.models import (
    ExtractionStatus,
    RefreshDisposition,
    RefreshResult,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.normalize import normalize_text
from nika_core.research.repository import ResearchRepository


class ResearchFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        validators: HttpValidators | None = None,
        policy: HttpFetchPolicy | None = None,
    ) -> HttpFetchResult: ...


def _source_title(url: str) -> str:
    parts = urlsplit(url)
    name = Path(unquote(parts.path)).name
    return name or parts.hostname or "web-source"


def _retry_delay(result: HttpFetchResult, attempt: int, policy: HttpFetchPolicy) -> float:
    if result.retry_after_seconds is not None:
        return min(result.retry_after_seconds, policy.max_backoff_seconds)
    exponential = policy.backoff_base_seconds * (2 ** max(attempt - 1, 0))
    return min(exponential, policy.max_backoff_seconds)


class HttpResearchService:
    def __init__(
        self,
        *,
        repository: ResearchRepository,
        network_repository: NetworkResearchRepository,
        blob_store: ContentAddressedBlobStore,
        fetcher: ResearchFetcher,
        policy: HttpFetchPolicy | None = None,
        document_limits: DocumentLimits | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._network = network_repository
        self._blobs = blob_store
        self._fetcher = fetcher
        self._policy = policy or HttpFetchPolicy()
        self._document_limits = document_limits
        self._sleeper = sleeper

    def register_source(self, source: SourceSpec):
        return self._network.register_source(source)

    def _record_fetch_attempt(
        self,
        *,
        source_id: str,
        attempt_number: int,
        result: HttpFetchResult,
        disposition: RefreshDisposition | None = None,
        task_id: str | None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        self._network.record_attempt(
            source_id=source_id,
            attempt_number=attempt_number,
            disposition=disposition or result.disposition,
            requested_url=result.requested_url,
            final_url=result.final_url,
            status_code=result.status_code,
            error_code=error_code if error_code is not None else result.error_code,
            error_message=message if message is not None else result.message,
            retryable=result.retryable,
            task_id=task_id,
        )

    def refresh_source(
        self,
        source_id: str,
        *,
        task_id: str | None = None,
    ) -> RefreshResult:
        state = self._network.get_source(source_id)
        validators = HttpValidators(etag=state.etag, last_modified=state.last_modified)
        result: HttpFetchResult | None = None
        attempts = 0
        for attempt in range(1, self._policy.max_attempts + 1):
            attempts = attempt
            result = self._fetcher.fetch(
                state.url,
                validators=validators,
                policy=self._policy,
            )
            if result.retryable and attempt < self._policy.max_attempts:
                self._record_fetch_attempt(
                    source_id=source_id,
                    attempt_number=attempt,
                    result=result,
                    task_id=task_id,
                )
                delay = _retry_delay(result, attempt, self._policy)
                if delay > 0:
                    self._sleeper(delay)
                continue
            break
        if result is None:
            raise RuntimeError("HTTP fetcher produced no result")

        if result.disposition is not RefreshDisposition.CHANGED:
            self._record_fetch_attempt(
                source_id=source_id,
                attempt_number=attempts,
                result=result,
                task_id=task_id,
            )
            self._network.finalize_source(
                source_id,
                disposition=result.disposition,
                final_url=result.final_url,
                status_code=result.status_code,
                etag=result.etag,
                last_modified=result.last_modified,
                error_code=result.error_code,
                error_message=result.message,
            )
            return RefreshResult(
                source_id=source_id,
                disposition=result.disposition,
                attempts=attempts,
                status_code=result.status_code,
                error_code=result.error_code,
                message=result.message,
            )

        if result.body is None or result.media_type is None:
            raise RuntimeError("successful HTTP fetch omitted body or media type")
        source = SourceSpec(
            source_id=state.source_id,
            workspace_id=state.workspace_id,
            kind=SourceKind.HTTP,
            locator=state.url,
        )
        artifact = self._blobs.put_bytes(
            state.workspace_id,
            result.body,
            max_bytes=self._policy.max_response_bytes,
        )
        title = _source_title(result.final_url)
        self._repository.record_artifact(
            source,
            artifact,
            media_type=result.media_type,
            original_name=title,
        )
        if artifact.raw_sha256 == state.current_raw_sha256:
            self._record_fetch_attempt(
                source_id=source_id,
                attempt_number=attempts,
                result=result,
                disposition=RefreshDisposition.UNCHANGED,
                task_id=task_id,
            )
            self._network.finalize_source(
                source_id,
                disposition=RefreshDisposition.UNCHANGED,
                final_url=result.final_url,
                status_code=result.status_code,
                etag=result.etag,
                last_modified=result.last_modified,
                current_raw_sha256=artifact.raw_sha256,
            )
            return RefreshResult(
                source_id=source_id,
                disposition=RefreshDisposition.UNCHANGED,
                attempts=attempts,
                status_code=result.status_code,
            )

        try:
            if is_document_media_type(result.media_type):
                blob_path = self._blobs.resolve(artifact)
                extractor, extractor_version = document_extractor_identity(
                    blob_path,
                    media_type=result.media_type,
                )
                extracted = extract_document_file(
                    blob_path,
                    limits=self._document_limits,
                    media_type=result.media_type,
                )
                extracted = replace(extracted, title=title)
            else:
                extractor, extractor_version = "nika-stdlib", "1"
                extracted = extract_text_payload(
                    result.body,
                    title=title,
                    media_type=result.media_type,
                )
        except (DocumentIngestionError, LocalIngestionError, BlobStoreError, ValueError) as exc:
            extraction = self._repository.record_extraction(
                artifact_id=artifact.artifact_id,
                extractor=locals().get("extractor", "unknown"),
                extractor_version=locals().get("extractor_version", "unknown"),
                status=ExtractionStatus.FAILED,
                normalized_text_sha256=None,
                detail=f"{type(exc).__name__}: {exc}"[:1000],
            )
            snapshot_id = self._network.record_snapshot(
                source_id=source_id,
                artifact_id=artifact.artifact_id,
                raw_sha256=artifact.raw_sha256,
                media_type=result.media_type,
                etag=result.etag,
                last_modified=result.last_modified,
                extraction_id=extraction.extraction_id,
                document_id=None,
            )
            self._record_fetch_attempt(
                source_id=source_id,
                attempt_number=attempts,
                result=result,
                disposition=RefreshDisposition.FAILED,
                task_id=task_id,
                error_code="extraction_failed",
                message=f"{type(exc).__name__}: {exc}"[:1000],
            )
            self._network.finalize_source(
                source_id,
                disposition=RefreshDisposition.FAILED,
                final_url=result.final_url,
                status_code=result.status_code,
                etag=result.etag,
                last_modified=result.last_modified,
                error_code="extraction_failed",
                error_message=f"{type(exc).__name__}: {exc}"[:1000],
            )
            return RefreshResult(
                source_id=source_id,
                disposition=RefreshDisposition.FAILED,
                attempts=attempts,
                status_code=result.status_code,
                snapshot_id=snapshot_id,
                error_code="extraction_failed",
                message=str(exc)[:1000],
            )

        normalized = normalize_text(extracted.text)
        status = extracted.status
        if status is ExtractionStatus.EXTRACTED and not normalized:
            status = ExtractionStatus.EMPTY
        normalized_sha256 = hashlib.sha256(normalized.encode()).hexdigest() if normalized else None
        extraction = self._repository.record_extraction(
            artifact_id=artifact.artifact_id,
            extractor=extracted.extractor,
            extractor_version=extracted.extractor_version,
            status=status,
            normalized_text_sha256=normalized_sha256,
        )

        corpus = None
        disposition = RefreshDisposition.CHANGED
        if status is ExtractionStatus.EXTRACTED:
            corpus = self._repository.ingest_document(source, extracted)
            self._repository.link_document_artifact(
                document_id=corpus.document.document_id,
                artifact_id=artifact.artifact_id,
                extraction_id=extraction.extraction_id,
            )
        elif result.media_type == "text/html" and b"<script" in result.body.casefold():
            disposition = RefreshDisposition.DYNAMIC_REQUIRED

        snapshot_id = self._network.record_snapshot(
            source_id=source_id,
            artifact_id=artifact.artifact_id,
            raw_sha256=artifact.raw_sha256,
            media_type=result.media_type,
            etag=result.etag,
            last_modified=result.last_modified,
            extraction_id=extraction.extraction_id,
            document_id=corpus.document.document_id if corpus is not None else None,
        )
        if corpus is not None:
            self._network.link_document_origin(
                document_id=corpus.document.document_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                locator=result.final_url,
            )
        self._record_fetch_attempt(
            source_id=source_id,
            attempt_number=attempts,
            result=result,
            disposition=disposition,
            task_id=task_id,
            error_code="dynamic_required" if disposition is RefreshDisposition.DYNAMIC_REQUIRED else None,
            message=(
                "static HTML yielded no indexable text; dynamic rendering is required"
                if disposition is RefreshDisposition.DYNAMIC_REQUIRED
                else ""
            ),
        )
        self._network.finalize_source(
            source_id,
            disposition=disposition,
            final_url=result.final_url,
            status_code=result.status_code,
            etag=result.etag,
            last_modified=result.last_modified,
            current_raw_sha256=artifact.raw_sha256,
            error_code="dynamic_required" if disposition is RefreshDisposition.DYNAMIC_REQUIRED else None,
            error_message=(
                "static HTML yielded no indexable text; dynamic rendering is required"
                if disposition is RefreshDisposition.DYNAMIC_REQUIRED
                else ""
            ),
        )
        return RefreshResult(
            source_id=source_id,
            disposition=disposition,
            attempts=attempts,
            status_code=result.status_code,
            snapshot_id=snapshot_id,
            document_id=corpus.document.document_id if corpus is not None else None,
            error_code="dynamic_required" if disposition is RefreshDisposition.DYNAMIC_REQUIRED else None,
        )
