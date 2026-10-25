from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import MediaSource, MediaSourceKind, ProcessingJob
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file
from nika_core.media.ocr import OCRPageRequest, TesseractOCRAdapter
from nika_core.media.ocr_repository import DurableOCRPage, OCRPageRepository, OCRPageState
from nika_core.media.process import ProcessResult
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import initialize_media_schema


_TSV = (
    b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    b"left\ttop\twidth\theight\tconf\ttext\n"
    b"5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t95\tbound\n"
)


class RecordingRunner:
    def __init__(self, *, mutate_source: Path | None = None) -> None:
        self.mutate_source = mutate_source
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd, timeout_seconds, env=None, cancel_event=None):
        del cwd, timeout_seconds, env, cancel_event
        normalized = tuple(str(part) for part in argv)
        self.calls.append(normalized)
        if self.mutate_source is not None:
            self.mutate_source.write_bytes(b"substituted-during-ocr")
        return ProcessResult(normalized, 0, _TSV, b"", 0.01)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state with spaces" / "ніка.db")
    store.initialize()
    initialize_media_schema(store)
    media = MediaRepository(store)
    media.put_source(MediaSource(source_id="s1", kind=MediaSourceKind.LOCAL_FILE, locator="local"))
    media.put_job(ProcessingJob(job_id="j1", source_id="s1", stage="ocr"))
    return store


def test_repository_binds_source_bytes_and_restart_rejects_substitution(tmp_path: Path) -> None:
    source = tmp_path / "сторінка з пробілами.png"
    source.write_bytes(b"synthetic-page-v1")
    original_sha256 = sha256_file(source)
    store = _store(tmp_path)
    repository = OCRPageRepository(store)
    repository.put(
        DurableOCRPage(
            page_id="p1",
            job_id="j1",
            ordinal=0,
            page_number=1,
            source_path=str(source),
            state=OCRPageState.RUNNING,
        )
    )

    stored = repository.list_for_job("j1")[0]
    assert stored.source_path == str(source.resolve())
    assert stored.source_sha256 == original_sha256
    request = stored.as_request(language="ukr+eng")
    assert request.source_sha256 == original_sha256
    assert request.image_path == source.resolve()

    source.write_bytes(b"synthetic-page-v2")
    reopened_store = SQLiteStore(store.path)
    reopened_store.initialize()
    initialize_media_schema(reopened_store)
    reopened = OCRPageRepository(reopened_store)
    assert reopened.pending_for_resume("j1") == ()

    failed = reopened.list_for_job("j1")[0]
    assert failed.state == OCRPageState.FAILED
    assert failed.error_code == MediaErrorCode.CHECKSUM_MISMATCH.value
    assert failed.source_sha256 == original_sha256
    assert "will not be retried" in (failed.error_message or "")


def test_restart_fails_closed_for_legacy_unbound_ocr_page(tmp_path: Path) -> None:
    source = tmp_path / "legacy.png"
    source.write_bytes(b"legacy-page")
    store = _store(tmp_path)
    legacy = DurableOCRPage(
        page_id="legacy-p1",
        job_id="j1",
        ordinal=0,
        page_number=1,
        source_path=str(source),
        state=OCRPageState.RUNNING,
    )
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO media_ocr_pages(page_id, job_id, ordinal, state, page_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                legacy.page_id,
                legacy.job_id,
                legacy.ordinal,
                legacy.state.value,
                legacy.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )

    repository = OCRPageRepository(store)
    assert repository.pending_for_resume("j1") == ()
    failed = repository.list_for_job("j1")[0]
    assert failed.state == OCRPageState.FAILED
    assert failed.error_code == MediaErrorCode.CHECKSUM_MISMATCH.value
    assert "identity is missing" in (failed.error_message or "")


def test_adapter_rejects_wrong_bound_digest_before_process(tmp_path: Path) -> None:
    source = tmp_path / "page.png"
    source.write_bytes(b"bound-page")
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"fixture")
    runner = RecordingRunner()
    adapter = TesseractOCRAdapter(executable=executable, runner=runner)

    with pytest.raises(MediaError) as exc:
        adapter.recognize_page(
            OCRPageRequest(
                page_number=1,
                image_path=source,
                source_sha256="0" * 64,
            ),
            cwd=tmp_path,
            timeout_seconds=5,
        )
    assert exc.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert runner.calls == []


def test_adapter_discards_result_if_original_source_changes_during_ocr(tmp_path: Path) -> None:
    source = tmp_path / "page.png"
    source.write_bytes(b"bound-page")
    source_sha256 = sha256_file(source)
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"fixture")
    runner = RecordingRunner(mutate_source=source)
    adapter = TesseractOCRAdapter(executable=executable, runner=runner)

    with pytest.raises(MediaError) as exc:
        adapter.recognize_page(
            OCRPageRequest(
                page_number=1,
                image_path=source,
                source_sha256=source_sha256,
            ),
            cwd=tmp_path,
            timeout_seconds=5,
        )
    assert exc.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert len(runner.calls) == 1
    snapshot_path = Path(runner.calls[0][1])
    assert snapshot_path.name == "input.png"
    assert snapshot_path != source.resolve()
    assert not snapshot_path.exists()


def test_concurrent_same_page_registration_converges_on_one_bound_source(tmp_path: Path) -> None:
    source = tmp_path / "same-page.png"
    source.write_bytes(b"same-source")
    store = _store(tmp_path)
    barrier = Barrier(2)

    def register() -> None:
        repository = OCRPageRepository(SQLiteStore(store.path))
        page = DurableOCRPage(
            page_id="p-concurrent",
            job_id="j1",
            ordinal=0,
            page_number=1,
            source_path=str(source),
        )
        barrier.wait()
        repository.put(page)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(register)
        second = pool.submit(register)
        first.result()
        second.result()

    stored = OCRPageRepository(store).list_for_job("j1")
    assert len(stored) == 1
    assert stored[0].source_sha256 == sha256_file(source)
