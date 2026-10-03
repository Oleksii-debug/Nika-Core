from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from nika_core.ui.bridge_models import UIResult

_MODEL_DIR = Path("voice") / "whisper"
_REQUIRED_FILES = ("encoder.onnx", "decoder.onnx", "tokens.txt")
_ONNX_FILE_LIMIT = 4 * 1024 * 1024 * 1024
_TOKENS_FILE_LIMIT = 64 * 1024 * 1024
_TOTAL_LIMIT = 8 * 1024 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_REPARSE_POINT = 0x400
_MAX_PATH_CHARS = 32_767


class PackagedVoiceModelSetup:
    """Explicit local-only first-run installer for the packaged Whisper files."""

    def __init__(self, data_root: Path) -> None:
        if type(data_root) is not Path:
            raise TypeError("data_root must be an exact pathlib.Path")
        root = data_root.expanduser()
        if not root.is_absolute():
            raise ValueError("data_root must be absolute")
        self._data_root = root
        self._restart_required = False

    def snapshot(self) -> dict[str, object]:
        state = self._installation_state()
        if self._restart_required and state == "installed":
            return self._public_state(
                status="restart_required",
                installed=True,
                can_import=False,
                restart_required=True,
                message=(
                    "Локальну голосову модель встановлено. Перезапустіть Nika Core, "
                    "щоб увімкнути голосовий ввід."
                ),
            )
        if state == "installed":
            return self._public_state(
                status="installed",
                installed=True,
                can_import=False,
                restart_required=False,
                message="Локальна голосова модель встановлена.",
            )
        if state == "partial":
            return self._public_state(
                status="partial",
                installed=False,
                can_import=False,
                restart_required=False,
                message=(
                    "Папка локальної голосової моделі вже існує, але встановлення "
                    "неповне або небезпечне. Автоматичний імпорт зупинено."
                ),
            )
        return self._public_state(
            status="missing",
            installed=False,
            can_import=sys.platform == "win32",
            restart_required=False,
            message=(
                "Локальна голосова модель ще не встановлена. Вкажіть повний шлях "
                "до папки з encoder.onnx, decoder.onnx і tokens.txt."
            ),
        )

    def install(self, payload: dict[str, Any]) -> UIResult:
        if type(payload) is not dict:
            raise TypeError("voice model import payload must be an exact dict")
        if set(payload) != {"source_root"}:
            raise ValueError("voice model import requires only source_root")
        source_text = payload.get("source_root")
        if type(source_text) is not str:
            raise TypeError("voice model source_root must be an exact string")
        if sys.platform != "win32":
            return self._result(
                "rejected",
                "Імпорт локальної голосової моделі доступний лише у Windows.",
            )

        state = self._installation_state()
        if state == "installed":
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="Локальна голосова модель уже встановлена.",
                focus_id="voice-heading",
            )
        if state == "partial":
            return self._result(
                "rejected",
                (
                    "Канонічна папка голосової моделі вже містить неповне або "
                    "небезпечне встановлення. Автоматичний імпорт не перезаписує його."
                ),
            )

        try:
            source_root = self._validate_source_root(source_text)
            source_files = self._validate_source_files(source_root)
            self._install_atomically(source_files)
        except _SetupInputError as exc:
            return self._result("rejected", exc.public_message)
        except (OSError, RuntimeError):
            return self._result(
                "failed",
                "Не вдалося безпечно встановити локальну голосову модель.",
            )

        self._restart_required = True
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                "Локальну голосову модель встановлено. Перезапустіть Nika Core, "
                "щоб увімкнути голосовий ввід."
            ),
            focus_id="voice-heading",
        )

    def _validate_source_root(self, source_text: str) -> Path:
        if (
            not source_text
            or source_text != source_text.strip()
            or len(source_text) > _MAX_PATH_CHARS
            or "\x00" in source_text
        ):
            raise _SetupInputError("Вкажіть коректний повний шлях до папки моделі.")
        source_root = Path(source_text).expanduser()
        if not source_root.is_absolute():
            raise _SetupInputError("Шлях до папки моделі має бути повним.")
        try:
            self._require_regular_directory(source_root)
            self._require_no_reparse_ancestors(source_root)
        except OSError as exc:
            raise _SetupInputError("Папку локальної голосової моделі не знайдено.") from exc
        return source_root

    def _validate_source_files(self, source_root: Path) -> dict[str, tuple[Path, os.stat_result]]:
        result: dict[str, tuple[Path, os.stat_result]] = {}
        total = 0
        for name in _REQUIRED_FILES:
            path = source_root / name
            try:
                evidence = os.lstat(path)
            except OSError as exc:
                raise _SetupInputError(
                    f"У вибраній папці немає обов'язкового файла {name}."
                ) from exc
            if (
                not stat.S_ISREG(evidence.st_mode)
                or stat.S_ISLNK(evidence.st_mode)
                or _is_reparse(evidence)
            ):
                raise _SetupInputError(
                    f"Файл {name} має бути звичайним локальним файлом."
                )
            limit = _TOKENS_FILE_LIMIT if name == "tokens.txt" else _ONNX_FILE_LIMIT
            if evidence.st_size <= 0 or evidence.st_size > limit:
                raise _SetupInputError(f"Розмір файла {name} виходить за безпечні межі.")
            total += evidence.st_size
            if total > _TOTAL_LIMIT:
                raise _SetupInputError("Загальний розмір голосової моделі завеликий.")
            result[name] = (path, evidence)
        return result

    def _install_atomically(
        self,
        source_files: dict[str, tuple[Path, os.stat_result]],
    ) -> None:
        voice_root = self._data_root / "voice"
        model_root = self._data_root / _MODEL_DIR
        voice_root.mkdir(parents=True, exist_ok=True)
        if _path_lexists(model_root):
            raise RuntimeError("canonical model target appeared during import")

        lock_path = voice_root / ".whisper-import.lock"
        lock_fd: int | None = None
        stage: Path | None = None
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            if _path_lexists(model_root):
                raise RuntimeError("canonical model target appeared during import")
            stage = Path(tempfile.mkdtemp(prefix=".whisper-import-", dir=voice_root))
            for name in _REQUIRED_FILES:
                source, expected = source_files[name]
                _copy_verified_regular_file(source, stage / name, expected)
            if _path_lexists(model_root):
                raise RuntimeError("canonical model target appeared during import")
            stage.rename(model_root)
            stage = None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)

    def _installation_state(self) -> str:
        model_root = self._data_root / _MODEL_DIR
        if not _path_lexists(model_root):
            return "missing"
        try:
            root_evidence = os.lstat(model_root)
        except OSError:
            return "partial"
        if (
            not stat.S_ISDIR(root_evidence.st_mode)
            or stat.S_ISLNK(root_evidence.st_mode)
            or _is_reparse(root_evidence)
        ):
            return "partial"
        for name in _REQUIRED_FILES:
            path = model_root / name
            try:
                evidence = os.lstat(path)
            except OSError:
                return "partial"
            if (
                not stat.S_ISREG(evidence.st_mode)
                or stat.S_ISLNK(evidence.st_mode)
                or _is_reparse(evidence)
                or evidence.st_size <= 0
            ):
                return "partial"
        return "installed"

    @staticmethod
    def _require_regular_directory(path: Path) -> None:
        evidence = os.lstat(path)
        if (
            not stat.S_ISDIR(evidence.st_mode)
            or stat.S_ISLNK(evidence.st_mode)
            or _is_reparse(evidence)
        ):
            raise OSError("source root is not a direct regular directory")

    @staticmethod
    def _require_no_reparse_ancestors(path: Path) -> None:
        resolved_parts = path.parts
        if not resolved_parts:
            raise OSError("source root has no path components")
        current = Path(resolved_parts[0])
        for part in resolved_parts[1:]:
            current = current / part
            evidence = os.lstat(current)
            if stat.S_ISLNK(evidence.st_mode) or _is_reparse(evidence):
                raise OSError("source path traverses an indirection")

    @staticmethod
    def _public_state(
        *,
        status: str,
        installed: bool,
        can_import: bool,
        restart_required: bool,
        message: str,
    ) -> dict[str, object]:
        return {
            "schema": "nika.packaged-voice-model-setup:v1",
            "status": status,
            "installed": installed,
            "can_import": can_import,
            "restart_required": restart_required,
            "message": message,
        }

    @staticmethod
    def _result(status: str, message: str) -> UIResult:
        return UIResult(
            request_id="desktop-handler",
            status=status,
            message=message,
            focus_id="voice-model-source",
        )


class _SetupInputError(ValueError):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


def _is_reparse(evidence: os.stat_result) -> bool:
    attributes = getattr(evidence, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _copy_verified_regular_file(
    source: Path,
    destination: Path,
    expected: os.stat_result,
) -> None:
    with source.open("rb", buffering=0) as reader:
        opened = os.fstat(reader.fileno())
        _require_same_regular_file(expected, opened)
        with destination.open("xb", buffering=0) as writer:
            copied = _stream_copy(reader, writer, expected.st_size)
            writer.flush()
            os.fsync(writer.fileno())
    if copied != expected.st_size:
        raise RuntimeError("source file length changed during import")


def _stream_copy(reader: BinaryIO, writer: BinaryIO, expected_size: int) -> int:
    copied = 0
    while True:
        chunk = reader.read(_COPY_CHUNK)
        if not chunk:
            break
        copied += len(chunk)
        if copied > expected_size:
            raise RuntimeError("source file grew during import")
        writer.write(chunk)
    return copied


def _require_same_regular_file(expected: os.stat_result, opened: os.stat_result) -> None:
    if (
        not stat.S_ISREG(opened.st_mode)
        or _is_reparse(opened)
        or opened.st_size != expected.st_size
        or opened.st_dev != expected.st_dev
        or opened.st_ino != expected.st_ino
    ):
        raise RuntimeError("source file identity changed before copy")
