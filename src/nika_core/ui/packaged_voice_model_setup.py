from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Lock
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

ModelImportSubmitter = Callable[[Coroutine[Any, Any, Any]], Future[Any]]


class PackagedVoiceModelSetup:
    """Accessible local-only first-run installer for packaged Whisper files."""

    def __init__(
        self,
        data_root: Path,
        *,
        submit: ModelImportSubmitter | None = None,
    ) -> None:
        if not isinstance(data_root, Path):
            raise TypeError("data_root must be a pathlib.Path")
        root = data_root.expanduser()
        if not root.is_absolute():
            raise ValueError("data_root must be absolute")
        if submit is not None and not callable(submit):
            raise TypeError("voice model setup submitter must be callable")

        self._data_root = root
        self._submit = submit
        self._lock = Lock()
        self._generation = 0
        self._active = False
        self._cancelling = False
        self._cancel_event: Event | None = None
        self._active_future: Future[Any] | None = None
        self._last_status: str | None = None
        self._last_message: str | None = None
        self._restart_required = False
        self._closed = False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            generation = self._generation
            active = self._active
            cancelling = self._cancelling
            restart_required = self._restart_required
            last_status = self._last_status
            last_message = self._last_message

        if active:
            return self._public_state(
                status="cancelling" if cancelling else "importing",
                generation=generation,
                active=True,
                installed=False,
                can_import=False,
                restart_required=False,
                message=(
                    "Скасування імпорту голосової моделі виконується."
                    if cancelling
                    else "Локальна голосова модель імпортується."
                ),
            )

        state = self._installation_state()
        if restart_required and state == "installed":
            return self._public_state(
                status="restart_required",
                generation=generation,
                active=False,
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
                generation=generation,
                active=False,
                installed=True,
                can_import=False,
                restart_required=False,
                message="Локальна голосова модель встановлена.",
            )
        if state == "partial":
            return self._public_state(
                status="partial",
                generation=generation,
                active=False,
                installed=False,
                can_import=False,
                restart_required=False,
                message=(
                    "Папка локальної голосової моделі вже існує, але встановлення "
                    "неповне або небезпечне. Автоматичний імпорт зупинено."
                ),
            )
        if last_status in {"failed", "cancelled"} and last_message:
            return self._public_state(
                status=last_status,
                generation=generation,
                active=False,
                installed=False,
                can_import=sys.platform == "win32" and not self._closed,
                restart_required=False,
                message=last_message,
            )
        return self._public_state(
            status="missing",
            generation=generation,
            active=False,
            installed=False,
            can_import=sys.platform == "win32" and not self._closed,
            restart_required=False,
            message=(
                "Локальна голосова модель ще не встановлена. Вкажіть повний шлях "
                "до папки з encoder.onnx, decoder.onnx і tokens.txt."
            ),
        )

    def start(self, payload: dict[str, Any]) -> UIResult:
        source_text = self._require_start_payload(payload)
        if sys.platform != "win32":
            return self._result(
                "rejected",
                "Імпорт локальної голосової моделі доступний лише у Windows.",
            )
        if self._submit is None:
            return self._result(
                "failed",
                "Фоновий імпорт голосової моделі недоступний.",
            )
        try:
            self._source_path_from_text(source_text)
        except _SetupInputError as exc:
            return self._result("rejected", exc.public_message)

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

        with self._lock:
            if self._closed:
                return self._result(
                    "rejected",
                    "Імпорт голосової моделі вже завершив роботу разом із застосунком.",
                )
            if self._active:
                return UIResult(
                    request_id="desktop-handler",
                    status="rejected",
                    message="Імпорт голосової моделі вже виконується.",
                    focus_id="voice-model-cancel",
                )
            self._generation += 1
            generation = self._generation
            cancel_event = Event()
            self._cancel_event = cancel_event
            self._active = True
            self._cancelling = False
            self._last_status = None
            self._last_message = None

        started_event = Event()
        coroutine = self._run_import(
            {"source_root": source_text},
            generation=generation,
            cancel_event=cancel_event,
            started_event=started_event,
        )
        try:
            future = self._submit(coroutine)
        except Exception:  # noqa: BLE001 - bounded packaged submit failure
            coroutine.close()
            with self._lock:
                if generation == self._generation:
                    self._active = False
                    self._cancel_event = None
                    self._last_status = "failed"
                    self._last_message = (
                        "Не вдалося запустити фоновий імпорт голосової моделі."
                    )
            return self._result(
                "failed",
                "Не вдалося запустити фоновий імпорт голосової моделі.",
            )
        if type(future) is not Future:
            coroutine.close()
            with self._lock:
                if generation == self._generation:
                    self._active = False
                    self._cancel_event = None
                    self._last_status = "failed"
                    self._last_message = (
                        "Не вдалося запустити фоновий імпорт голосової моделі."
                    )
            return self._result(
                "failed",
                "Не вдалося запустити фоновий імпорт голосової моделі.",
            )

        with self._lock:
            if generation != self._generation or not self._active:
                future.cancel()
                return self._result(
                    "failed",
                    "Стан імпорту голосової моделі змінився до запуску.",
                )
            self._active_future = future
        future.add_done_callback(
            lambda done, identity=generation, started=started_event: (
                self._submitted_done(identity, started, done)
            )
        )

        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Імпорт локальної голосової моделі розпочато.",
            focus_id="voice-model-cancel",
        )

    def _submitted_done(
        self,
        generation: int,
        started_event: Event,
        future: Future[Any],
    ) -> None:
        with self._lock:
            if self._active_future is not future:
                return
            self._active_future = None
            if generation != self._generation or not self._active:
                return
            cancel_event = self._cancel_event

        if future.cancelled():
            if cancel_event is not None:
                cancel_event.set()
            if started_event.is_set():
                with self._lock:
                    if generation == self._generation and self._active:
                        self._cancelling = True
                return
            status = "cancelled"
            message = "Імпорт голосової моделі скасовано."
        else:
            try:
                failure = future.exception()
            except Exception:  # noqa: BLE001 - untrusted Future completion boundary
                failure = RuntimeError("voice model import Future inspection failed")
            if failure is None:
                status = "failed"
                message = "Фоновий імпорт завершився без коректного стану."
            else:
                if cancel_event is not None:
                    cancel_event.set()
                status = "failed"
                message = "Не вдалося безпечно завершити фоновий імпорт моделі."

        with self._lock:
            if generation != self._generation or not self._active:
                return
            self._active = False
            self._cancelling = False
            self._cancel_event = None
            self._restart_required = False
            self._last_status = status
            self._last_message = message

    def cancel(self, payload: dict[str, Any]) -> UIResult:
        if type(payload) is not dict:
            raise TypeError("voice model cancel payload must be an exact dict")
        if payload:
            raise ValueError("voice model cancel does not accept payload authority")
        with self._lock:
            if not self._active or self._cancel_event is None:
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Активного імпорту голосової моделі немає.",
                    focus_id="voice-model-import",
                )
            if self._cancelling:
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Скасування імпорту голосової моделі вже запитано.",
                    focus_id="voice-model-heading",
                )
            self._cancelling = True
            self._cancel_event.set()
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Скасування імпорту голосової моделі запитано.",
            focus_id="voice-model-heading",
        )

    def install(self, payload: dict[str, Any]) -> UIResult:
        """Synchronous core used by focused tests; packaged UI calls start()."""

        source_text = self._require_start_payload(payload)
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
            self._perform_install(source_text, cancel_event=None)
        except _SetupInputError as exc:
            return self._result("rejected", exc.public_message)
        except _SetupCancelled:
            return self._result("rejected", "Імпорт голосової моделі скасовано.")
        except (OSError, RuntimeError):
            return self._result(
                "failed",
                "Не вдалося безпечно встановити локальну голосову модель.",
            )

        with self._lock:
            self._restart_required = True
            self._last_status = None
            self._last_message = None
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                "Локальну голосову модель встановлено. Перезапустіть Nika Core, "
                "щоб увімкнути голосовий ввід."
            ),
            focus_id="voice-heading",
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._active and self._cancel_event is not None:
                self._cancelling = True
                self._cancel_event.set()

    async def _run_import(
        self,
        payload: dict[str, Any],
        *,
        generation: int,
        cancel_event: Event,
        started_event: Event,
    ) -> None:
        terminal_status: str
        terminal_message: str
        restart_required = False
        cancellation: asyncio.CancelledError | None = None
        started_event.set()
        source_text = self._require_start_payload(payload)
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._perform_install,
                source_text,
                cancel_event=cancel_event,
            )
        )
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            cancellation = exc
            cancel_event.set()
            try:
                await asyncio.shield(worker)
            except _SetupCancelled:
                terminal_status = "cancelled"
                terminal_message = "Імпорт голосової моделі скасовано."
            except _SetupInputError as worker_error:
                terminal_status = "failed"
                terminal_message = worker_error.public_message
            except (OSError, RuntimeError):
                terminal_status = "failed"
                terminal_message = (
                    "Не вдалося безпечно встановити локальну голосову модель."
                )
            else:
                terminal_status = "restart_required"
                terminal_message = (
                    "Локальну голосову модель встановлено. Перезапустіть Nika Core, "
                    "щоб увімкнути голосовий ввід."
                )
                restart_required = True
        except _SetupCancelled:
            terminal_status = "cancelled"
            terminal_message = "Імпорт голосової моделі скасовано."
        except _SetupInputError as exc:
            terminal_status = "failed"
            terminal_message = exc.public_message
        except (OSError, RuntimeError):
            terminal_status = "failed"
            terminal_message = "Не вдалося безпечно встановити локальну голосову модель."
        else:
            terminal_status = "restart_required"
            terminal_message = (
                "Локальну голосову модель встановлено. Перезапустіть Nika Core, "
                "щоб увімкнути голосовий ввід."
            )
            restart_required = True

        with self._lock:
            if generation == self._generation:
                self._active = False
                self._cancelling = False
                self._cancel_event = None
                self._restart_required = restart_required
                self._last_status = terminal_status
                self._last_message = terminal_message

        if cancellation is not None:
            raise cancellation

    def _perform_install(
        self,
        source_text: str,
        *,
        cancel_event: Event | None,
    ) -> None:
        _check_cancel(cancel_event)
        source_root = self._validate_source_root(source_text)
        source_files = self._validate_source_files(source_root)
        _check_cancel(cancel_event)
        self._install_atomically(source_files, cancel_event=cancel_event)

    @staticmethod
    def _require_start_payload(payload: dict[str, Any]) -> str:
        if type(payload) is not dict:
            raise TypeError("voice model import payload must be an exact dict")
        if set(payload) != {"source_root"}:
            raise ValueError("voice model import requires only source_root")
        source_text = payload.get("source_root")
        if type(source_text) is not str:
            raise TypeError("voice model source_root must be an exact string")
        return source_text

    def _source_path_from_text(self, source_text: str) -> Path:
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
        if ".." in source_root.parts:
            raise _SetupInputError("Шлях до папки моделі не може містити переходи ..")
        if source_root.anchor.startswith("\\\\"):
            raise _SetupInputError(
                "Мережевий UNC-шлях не дозволений для локальної голосової моделі."
            )
        return source_root

    def _validate_source_root(self, source_text: str) -> Path:
        source_root = self._source_path_from_text(source_text)
        try:
            self._require_regular_directory(source_root)
            self._require_no_reparse_ancestors(source_root)
        except OSError as exc:
            raise _SetupInputError("Папку локальної голосової моделі не знайдено.") from exc
        return source_root

    def _validate_source_files(
        self,
        source_root: Path,
    ) -> dict[str, tuple[Path, os.stat_result]]:
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
        *,
        cancel_event: Event | None,
    ) -> None:
        voice_root = self._data_root / "voice"
        model_root = self._data_root / _MODEL_DIR
        voice_root.mkdir(parents=True, exist_ok=True)
        self._require_regular_directory(voice_root)
        self._require_no_reparse_ancestors(voice_root)
        if _path_lexists(model_root):
            raise RuntimeError("canonical model target appeared during import")

        stage: Path | None = Path(
            tempfile.mkdtemp(prefix=".whisper-import-", dir=voice_root)
        )
        try:
            for name in _REQUIRED_FILES:
                _check_cancel(cancel_event)
                source, expected = source_files[name]
                _copy_verified_regular_file(
                    source,
                    stage / name,
                    expected,
                    cancel_event=cancel_event,
                )
            _check_cancel(cancel_event)
            if _path_lexists(model_root):
                raise RuntimeError("canonical model target appeared during import")
            stage.rename(model_root)
            stage = None
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)

    def _installation_state(self) -> str:
        model_root = self._data_root / _MODEL_DIR
        if not _path_lexists(model_root):
            return "missing"
        try:
            root_evidence = os.lstat(model_root)
            self._require_no_reparse_ancestors(model_root)
        except OSError:
            return "partial"
        if (
            not stat.S_ISDIR(root_evidence.st_mode)
            or stat.S_ISLNK(root_evidence.st_mode)
            or _is_reparse(root_evidence)
        ):
            return "partial"
        total = 0
        for name in _REQUIRED_FILES:
            path = model_root / name
            try:
                evidence = os.lstat(path)
            except OSError:
                return "partial"
            limit = _TOKENS_FILE_LIMIT if name == "tokens.txt" else _ONNX_FILE_LIMIT
            if (
                not stat.S_ISREG(evidence.st_mode)
                or stat.S_ISLNK(evidence.st_mode)
                or _is_reparse(evidence)
                or evidence.st_size <= 0
                or evidence.st_size > limit
            ):
                return "partial"
            total += evidence.st_size
            if total > _TOTAL_LIMIT:
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
            raise OSError("path is not a direct regular directory")

    @staticmethod
    def _require_no_reparse_ancestors(path: Path) -> None:
        parts = path.parts
        if not parts:
            raise OSError("path has no components")
        current = Path(parts[0])
        for part in parts[1:]:
            current = current / part
            evidence = os.lstat(current)
            if stat.S_ISLNK(evidence.st_mode) or _is_reparse(evidence):
                raise OSError("path traverses an indirection")

    @staticmethod
    def _public_state(
        *,
        status: str,
        generation: int,
        active: bool,
        installed: bool,
        can_import: bool,
        restart_required: bool,
        message: str,
    ) -> dict[str, object]:
        return {
            "schema": "nika.packaged-voice-model-setup:v1",
            "status": status,
            "generation": generation,
            "active": active,
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


class _SetupCancelled(RuntimeError):
    pass


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _SetupCancelled("voice model import cancelled")


def _is_reparse(evidence: os.stat_result) -> bool:
    attributes = getattr(evidence, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _copy_verified_regular_file(
    source: Path,
    destination: Path,
    expected: os.stat_result,
    *,
    cancel_event: Event | None,
) -> None:
    _check_cancel(cancel_event)
    with source.open("rb", buffering=0) as reader:
        opened = os.fstat(reader.fileno())
        _require_same_regular_file(expected, opened)
        with destination.open("xb", buffering=0) as writer:
            copied = _stream_copy(
                reader,
                writer,
                expected.st_size,
                cancel_event=cancel_event,
            )
            _require_same_regular_file(expected, os.fstat(reader.fileno()))
            _check_cancel(cancel_event)
            writer.flush()
            os.fsync(writer.fileno())
    if copied != expected.st_size:
        raise RuntimeError("source file length changed during import")


def _stream_copy(
    reader: BinaryIO,
    writer: BinaryIO,
    expected_size: int,
    *,
    cancel_event: Event | None,
) -> int:
    copied = 0
    while True:
        _check_cancel(cancel_event)
        chunk = reader.read(_COPY_CHUNK)
        if not chunk:
            break
        copied += len(chunk)
        if copied > expected_size:
            raise RuntimeError("source file grew during import")
        written = writer.write(chunk)
        if written != len(chunk):
            raise RuntimeError("destination file accepted a partial write")
    return copied


def _require_same_regular_file(
    expected: os.stat_result,
    opened: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(opened.st_mode)
        or _is_reparse(opened)
        or opened.st_size != expected.st_size
        or opened.st_dev != expected.st_dev
        or opened.st_ino != expected.st_ino
        or opened.st_mtime_ns != expected.st_mtime_ns
    ):
        raise RuntimeError("source file identity changed during import")
