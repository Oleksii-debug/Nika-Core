from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import MediaSource, MediaSourceKind, ProcessingJob
from nika_core.media.corrector import RevisionCorrector, normalize_text
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file
from nika_core.media.ocr import OCRPageRequest, TesseractOCRAdapter
from nika_core.media.ocr_repository import DurableOCRPage, OCRPageRepository, OCRPageState
from nika_core.media.process import ProcessResult
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import MEDIA_SCHEMA_VERSION, initialize_media_schema


class FakeRunner:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd, timeout_seconds, env=None, cancel_event=None):
        del cwd, timeout_seconds, env, cancel_event
        normalized = tuple(str(part) for part in argv)
        self.calls.append(normalized)
        return ProcessResult(normalized, 0, self.stdout, b"", 0.01)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    return store


def test_media_schema_batch_c_is_current(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        version = conn.execute("SELECT MAX(version) FROM media_schema_migrations").fetchone()[0]
    assert version == MEDIA_SCHEMA_VERSION == 3


def test_tesseract_adapter_uses_fixed_argv_and_parses_tsv(tmp_path: Path) -> None:
    image = tmp_path / "page 1.png"
    image.write_bytes(b"not-a-real-image-fixture")
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"fixture")
    payload = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        b"left\ttop\twidth\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tHello\n"
        b"5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t80\tworld\n"
    )
    runner = FakeRunner(payload)
    adapter = TesseractOCRAdapter(executable=executable, runner=runner)
    source_sha256 = sha256_file(image)
    page = adapter.recognize_page(
        OCRPageRequest(
            page_number=1,
            image_path=image,
            source_sha256=source_sha256,
            language="ukr+eng",
        ),
        cwd=tmp_path,
        timeout_seconds=5,
    )
    assert page.text == "Hello world"
    assert page.confidence == pytest.approx(0.85)
    assert page.source_sha256 == source_sha256
    assert runner.calls[0][0] == str(executable.resolve())
    assert Path(runner.calls[0][1]).name == "input.png"
    assert Path(runner.calls[0][1]) != image.resolve()
    assert runner.calls[0][2:] == ("stdout", "-l", "ukr+eng", "tsv")


def test_tesseract_missing_is_explicit_and_never_downloads(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("nika_core.media.ocr.shutil.which", lambda _name: None)
    adapter = TesseractOCRAdapter(executable="tesseract")
    image = tmp_path / "page.png"
    image.write_bytes(b"fixture")
    with pytest.raises(MediaError) as exc:
        adapter.recognize_page(
            OCRPageRequest(
                page_number=1,
                image_path=image,
                source_sha256=sha256_file(image),
            ),
            cwd=tmp_path,
            timeout_seconds=5,
        )
    assert exc.value.code == MediaErrorCode.COMPONENT_MISSING
    assert "will not download" in str(exc.value)


def test_ocr_restart_requeues_running_page_but_keeps_completed_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    media = MediaRepository(store)
    media.put_source(MediaSource(source_id="s1", kind=MediaSourceKind.LOCAL_FILE, locator="local"))
    media.put_job(ProcessingJob(job_id="j1", source_id="s1", stage="ocr"))
    source = tmp_path / "page 1.png"
    source.write_bytes(b"synthetic-ocr-page")
    pages = OCRPageRepository(store)
    pages.put(
        DurableOCRPage(
            page_id="p1",
            job_id="j1",
            ordinal=0,
            page_number=1,
            source_path=str(source),
            state=OCRPageState.RUNNING,
        )
    )
    resumed = pages.pending_for_resume("j1")
    assert resumed[0].state == OCRPageState.PENDING
    assert resumed[0].error_code == "restart_reconciliation_required"
    assert resumed[0].source_sha256 == sha256_file(source)

    completed = resumed[0].model_copy(update={"state": OCRPageState.COMPLETED})
    pages.put(completed)
    with pytest.raises(ValueError, match="immutable"):
        pages.put(completed.model_copy(update={"error_message": "mutated"}))


def test_corrector_normalizes_nfc_whitespace_and_preserves_terms() -> None:
    original = "  Cafe\u0301   Nika   Core \r\n\r\n\r\n  next  "
    result = normalize_text(original, protected_terms=("Nika   Core",))
    assert result.normalized == "Café Nika   Core\n\nnext"
    assert result.changed is True


def test_corrector_appends_revision_without_overwriting_original(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = MediaRepository(store)
    corrector = RevisionCorrector(repository)
    original = "A   B"
    first = corrector.append_deterministic_revision(
        artifact_id="artifact-1",
        original_text=original,
    )
    second = corrector.append_deterministic_revision(
        artifact_id="artifact-1",
        original_text=first.text + "   C",
    )
    revisions = repository.revisions("artifact-1")
    assert original == "A   B"
    assert first.text == "A B"
    assert second.parent_revision_id == first.revision_id
    assert [item.ordinal for item in revisions] == [0, 1]
