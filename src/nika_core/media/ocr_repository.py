from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import OCRPage
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file
from nika_core.media.ocr import OCRPageRequest


class OCRPageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DurableOCRPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page_id: str
    job_id: str
    ordinal: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_path: str
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    state: OCRPageState = OCRPageState.PENDING
    result: OCRPage | None = None
    error_code: str | None = None
    error_message: str | None = None

    def as_request(self, *, language: str = "eng") -> OCRPageRequest:
        if self.source_sha256 is None:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR page has no durable source identity and cannot be executed",
            )
        return OCRPageRequest(
            page_number=self.page_number,
            image_path=Path(self.source_path),
            source_sha256=self.source_sha256,
            language=language,
        )


class OCRPageRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def put(self, page: DurableOCRPage) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT page_json FROM media_ocr_pages WHERE page_id = ?",
                (page.page_id,),
            ).fetchone()
            if existing is None:
                page = self._bind_new_source(page)
            else:
                current = DurableOCRPage.model_validate_json(existing["page_json"])
                if current.state == OCRPageState.COMPLETED:
                    if current != page:
                        raise ValueError("completed OCR pages are immutable")
                    return
                page = self._preserve_bound_identity(current, page)
            self._upsert(conn, page)

    def list_for_job(self, job_id: str) -> tuple[DurableOCRPage, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT page_json FROM media_ocr_pages WHERE job_id = ? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        return tuple(DurableOCRPage.model_validate_json(row["page_json"]) for row in rows)

    def pending_for_resume(self, job_id: str) -> tuple[DurableOCRPage, ...]:
        result: list[DurableOCRPage] = []
        for page in self.list_for_job(job_id):
            if page.state in {OCRPageState.COMPLETED, OCRPageState.CANCELLED}:
                continue
            source_failure = self._source_failure_after_restart(page)
            if source_failure is not None:
                self._write_reconciliation_failure(page, *source_failure)
                continue
            if page.state == OCRPageState.RUNNING:
                page = page.model_copy(
                    update={
                        "state": OCRPageState.PENDING,
                        "error_code": "restart_reconciliation_required",
                        "error_message": (
                            "OCR page was running during restart and will retry "
                            "from its bound page source."
                        ),
                    }
                )
                self.put(page)
            result.append(page)
        return tuple(result)

    @staticmethod
    def _bind_new_source(page: DurableOCRPage) -> DurableOCRPage:
        try:
            source = Path(page.source_path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise MediaError(
                MediaErrorCode.SOURCE_NOT_FOUND,
                "OCR source file is missing",
            ) from exc
        if not source.is_file():
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                "OCR source must be a regular file",
            )
        observed_sha256 = sha256_file(source)
        if page.source_sha256 is not None and page.source_sha256 != observed_sha256:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR source bytes do not match the supplied source identity",
            )
        if page.result is not None and page.result.source_sha256 != observed_sha256:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR result provenance does not match the bound source identity",
            )
        return page.model_copy(
            update={
                "source_path": str(source),
                "source_sha256": observed_sha256,
            }
        )

    @staticmethod
    def _preserve_bound_identity(
        current: DurableOCRPage,
        candidate: DurableOCRPage,
    ) -> DurableOCRPage:
        immutable_identity = (
            "job_id",
            "ordinal",
            "page_number",
            "source_path",
        )
        if any(
            getattr(current, field) != getattr(candidate, field)
            for field in immutable_identity
        ):
            raise ValueError("OCR page durable identity is immutable")
        if current.source_sha256 is None:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "legacy OCR page has no durable source identity; "
                "requeue it with a new page identity",
            )
        if (
            candidate.source_sha256 is not None
            and candidate.source_sha256 != current.source_sha256
        ):
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR page source identity cannot be changed",
            )
        if (
            candidate.result is not None
            and candidate.result.source_sha256 != current.source_sha256
        ):
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR result provenance does not match the bound source identity",
            )
        return candidate.model_copy(update={"source_sha256": current.source_sha256})

    @staticmethod
    def _source_failure_after_restart(
        page: DurableOCRPage,
    ) -> tuple[str, str] | None:
        if page.source_sha256 is None:
            return (
                MediaErrorCode.CHECKSUM_MISMATCH.value,
                "OCR source identity is missing after restart; page will not be retried.",
            )
        try:
            observed_sha256 = sha256_file(Path(page.source_path))
        except MediaError as exc:
            if exc.code == MediaErrorCode.SOURCE_NOT_FOUND:
                return (
                    MediaErrorCode.SOURCE_NOT_FOUND.value,
                    "OCR source is unavailable after restart; page will not be retried.",
                )
            raise
        if observed_sha256 != page.source_sha256:
            return (
                MediaErrorCode.CHECKSUM_MISMATCH.value,
                "OCR source bytes changed after they were bound; page will not be retried.",
            )
        return None

    def _write_reconciliation_failure(
        self,
        page: DurableOCRPage,
        error_code: str,
        error_message: str,
    ) -> None:
        failed = page.model_copy(
            update={
                "state": OCRPageState.FAILED,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT page_json FROM media_ocr_pages WHERE page_id = ?",
                (page.page_id,),
            ).fetchone()
            if row is None:
                return
            current = DurableOCRPage.model_validate_json(row["page_json"])
            if current.state in {OCRPageState.COMPLETED, OCRPageState.CANCELLED}:
                return
            if current.source_sha256 != page.source_sha256:
                raise MediaError(
                    MediaErrorCode.CHECKSUM_MISMATCH,
                    "OCR page source identity changed during restart reconciliation",
                )
            self._upsert(conn, failed)

    @staticmethod
    def _upsert(conn, page: DurableOCRPage) -> None:
        conn.execute(
            """INSERT INTO media_ocr_pages(page_id, job_id, ordinal, state, page_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                state = excluded.state,
                page_json = excluded.page_json,
                updated_at = excluded.updated_at""",
            (
                page.page_id,
                page.job_id,
                page.ordinal,
                page.state.value,
                page.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
