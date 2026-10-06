from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import OCRPage


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
    state: OCRPageState = OCRPageState.PENDING
    result: OCRPage | None = None
    error_code: str | None = None
    error_message: str | None = None


class OCRPageRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def put(self, page: DurableOCRPage) -> None:
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT page_json FROM media_ocr_pages WHERE page_id = ?",
                (page.page_id,),
            ).fetchone()
            if existing is not None:
                current = DurableOCRPage.model_validate_json(existing["page_json"])
                if current.state == OCRPageState.COMPLETED and current != page:
                    raise ValueError("completed OCR pages are immutable")
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
            if page.state == OCRPageState.RUNNING:
                page = page.model_copy(
                    update={
                        "state": OCRPageState.PENDING,
                        "error_code": "restart_reconciliation_required",
                        "error_message": "OCR page was running during restart and will retry from its page boundary.",
                    }
                )
                self.put(page)
            result.append(page)
        return tuple(result)
