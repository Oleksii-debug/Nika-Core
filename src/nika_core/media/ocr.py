from __future__ import annotations

import csv
import io
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from nika_core.media.contracts import EngineDescriptor, OCRPage
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file
from nika_core.media.process import SafeProcessRunner


class OCRPageRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    image_path: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str = Field(default="eng", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_+.-]+$")


class OCREnginePort(Protocol):
    engine_id: str

    def recognize_page(
        self,
        request: OCRPageRequest,
        *,
        cwd: Path,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> OCRPage: ...


class TesseractOCRAdapter:
    """Optional external Tesseract adapter; it never installs engines or language data."""

    engine_id = "tesseract"

    def __init__(
        self,
        *,
        executable: Path | str = "tesseract",
        runner: SafeProcessRunner | None = None,
    ) -> None:
        self._executable = str(executable)
        self._runner = runner or SafeProcessRunner(max_output_bytes=8 * 1024 * 1024)

    def descriptor(self, *, cwd: Path, timeout_seconds: float = 10) -> EngineDescriptor:
        executable = self._resolve_executable()
        result = self._runner.run(
            (executable, "--version"),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        first = result.stdout.decode("utf-8", errors="replace").splitlines()
        version = first[0].strip() if first else "unknown"
        executable_sha256 = None
        resolved = Path(executable)
        if resolved.is_file():
            executable_sha256 = sha256_file(resolved)
        return EngineDescriptor(
            engine_id=self.engine_id,
            name="Tesseract OCR",
            version=version,
            license_id="Apache-2.0",
            source_reference="https://github.com/tesseract-ocr/tesseract",
            executable_sha256=executable_sha256,
            build_configuration=None,
        )

    def recognize_page(
        self,
        request: OCRPageRequest,
        *,
        cwd: Path,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> OCRPage:
        try:
            image = request.image_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise MediaError(
                MediaErrorCode.SOURCE_NOT_FOUND,
                "OCR source file is missing",
            ) from exc
        if not image.is_file():
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                "OCR input must be a regular file",
            )

        source_sha256 = sha256_file(image)
        if source_sha256 != request.source_sha256:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR source bytes do not match the durable source identity",
            )

        executable = self._resolve_executable()
        suffix = image.suffix if image.suffix else ".img"
        try:
            with tempfile.TemporaryDirectory(prefix="nika-ocr-", dir=cwd) as temp_dir:
                snapshot = Path(temp_dir) / f"input{suffix}"
                shutil.copyfile(image, snapshot)
                if sha256_file(snapshot) != request.source_sha256:
                    raise MediaError(
                        MediaErrorCode.CHECKSUM_MISMATCH,
                        "OCR source changed while creating the recognition snapshot",
                    )
                result = self._runner.run(
                    (
                        executable,
                        str(snapshot),
                        "stdout",
                        "-l",
                        request.language,
                        "tsv",
                    ),
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    cancel_event=cancel_event,
                )
                if sha256_file(snapshot) != request.source_sha256:
                    raise MediaError(
                        MediaErrorCode.CHECKSUM_MISMATCH,
                        "OCR recognition snapshot changed during execution",
                    )
        except MediaError:
            raise
        except OSError as exc:
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                "OCR source could not be snapshotted for recognition",
            ) from exc

        if sha256_file(image) != request.source_sha256:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "OCR source changed during recognition; result was discarded",
            )

        text, confidence = _parse_tesseract_tsv(result.stdout)
        return OCRPage(
            page_number=request.page_number,
            text=text,
            confidence=confidence,
            source_sha256=request.source_sha256,
        )

    def _resolve_executable(self) -> str:
        candidate = Path(self._executable)
        if candidate.parent != Path(".") or candidate.is_absolute():
            if not candidate.resolve().is_file():
                raise MediaError(
                    MediaErrorCode.COMPONENT_MISSING,
                    "Tesseract executable is missing",
                )
            return str(candidate.resolve())
        located = shutil.which(self._executable)
        if located is None:
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                "Tesseract OCR is not installed or discoverable; "
                "Nika will not download it automatically",
            )
        return located


def _parse_tesseract_tsv(payload: bytes) -> tuple[str, float | None]:
    decoded = payload.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(decoded), delimiter="\t")
    words: list[str] = []
    confidences: list[float] = []
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        words.append(text)
        raw_confidence = (row.get("conf") or "").strip()
        try:
            confidence = float(raw_confidence)
        except ValueError:
            continue
        if confidence >= 0:
            confidences.append(min(confidence, 100.0) / 100.0)
    average = sum(confidences) / len(confidences) if confidences else None
    return " ".join(words), average
