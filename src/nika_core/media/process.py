from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.privacy import redact_text


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float


class _BoundedReader(threading.Thread):
    def __init__(self, stream, *, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self.data = bytearray()
        self.exceeded = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = self._limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.exceeded = True
                    return
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


class SafeProcessRunner:
    def __init__(self, *, max_output_bytes: int = 4 * 1024 * 1024) -> None:
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        watched_paths: tuple[Path, ...] = (),
        max_watched_file_bytes: int | None = None,
    ) -> ProcessResult:
        normalized = tuple(str(part) for part in argv)
        self._validate_argv(normalized)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_watched_file_bytes is not None and max_watched_file_bytes <= 0:
            raise ValueError("max_watched_file_bytes must be positive")
        if watched_paths and max_watched_file_bytes is None:
            raise ValueError("watched_paths require max_watched_file_bytes")
        resolved_cwd = cwd.resolve(strict=True)
        if not resolved_cwd.is_dir():
            raise ValueError("cwd must be a directory")
        bounded_paths = tuple(
            self._bounded_watch_path(path, cwd=resolved_cwd) for path in watched_paths
        )

        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        started = time.monotonic()
        process = subprocess.Popen(
            normalized,
            cwd=resolved_cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_reader = _BoundedReader(process.stdout, limit=self._max_output_bytes)
        stderr_reader = _BoundedReader(process.stderr, limit=self._max_output_bytes)
        stdout_reader.start()
        stderr_reader.start()

        deadline = started + timeout_seconds
        failure: MediaError | None = None
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                failure = MediaError(
                    MediaErrorCode.PROCESS_CANCELLED,
                    "media subprocess was cancelled",
                )
                self._terminate_tree(process)
                break
            if self._watched_file_limit_exceeded(
                bounded_paths,
                max_bytes=max_watched_file_bytes,
            ):
                failure = MediaError(
                    MediaErrorCode.SOURCE_TOO_LARGE,
                    "media subprocess output exceeded the configured byte limit",
                )
                self._terminate_tree(process)
                break
            if stdout_reader.exceeded or stderr_reader.exceeded:
                failure = self._output_limit_error()
                self._terminate_tree(process)
                break
            if time.monotonic() >= deadline:
                failure = MediaError(
                    MediaErrorCode.PROCESS_TIMEOUT,
                    "media subprocess exceeded its deadline",
                    retryable=True,
                )
                self._terminate_tree(process)
                break
            time.sleep(0.01)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill_tree(process)
            process.wait(timeout=5)

        stdout_reader.join(timeout=2)
        stderr_reader.join(timeout=2)
        elapsed = time.monotonic() - started
        if failure is None and self._watched_file_limit_exceeded(
            bounded_paths,
            max_bytes=max_watched_file_bytes,
        ):
            failure = MediaError(
                MediaErrorCode.SOURCE_TOO_LARGE,
                "media subprocess output exceeded the configured byte limit",
            )
        if failure is None and (stdout_reader.exceeded or stderr_reader.exceeded):
            failure = self._output_limit_error()
        if failure is not None:
            raise failure

        result = ProcessResult(
            argv=tuple(redact_text(part) for part in normalized),
            returncode=int(process.returncode or 0),
            stdout=bytes(stdout_reader.data),
            stderr=bytes(stderr_reader.data),
            elapsed_seconds=elapsed,
        )
        if result.returncode != 0:
            stderr = redact_text(result.stderr.decode("utf-8", errors="replace"))
            raise MediaError(
                MediaErrorCode.PROCESS_FAILED,
                f"media subprocess failed with exit code {result.returncode}: {stderr[:800]}",
                retryable=False,
            )
        return result

    @staticmethod
    def _bounded_watch_path(path: Path, *, cwd: Path) -> Path:
        candidate = path if path.is_absolute() else cwd / path
        parent = candidate.parent.resolve(strict=True)
        try:
            parent.relative_to(cwd)
        except ValueError as exc:
            raise MediaError(
                MediaErrorCode.PATH_ESCAPE,
                "watched media path escapes subprocess cwd",
            ) from exc
        return parent / candidate.name

    @staticmethod
    def _watched_file_limit_exceeded(
        paths: tuple[Path, ...],
        *,
        max_bytes: int | None,
    ) -> bool:
        if max_bytes is None:
            return False
        for path in paths:
            if not path.exists():
                continue
            if path.is_symlink():
                raise MediaError(
                    MediaErrorCode.PATH_ESCAPE,
                    "watched media output must not be a symbolic link",
                )
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise MediaError(
                    MediaErrorCode.INVALID_SOURCE,
                    "watched media output must be a regular file",
                )
            if resolved.stat().st_size > max_bytes:
                return True
        return False

    @staticmethod
    def _output_limit_error() -> MediaError:
        return MediaError(
            MediaErrorCode.OUTPUT_LIMIT,
            "media subprocess exceeded the configured output limit",
        )

    @staticmethod
    def _validate_argv(argv: tuple[str, ...]) -> None:
        if not argv or not argv[0].strip():
            raise ValueError("argv must contain an executable")
        for part in argv:
            if "\x00" in part:
                raise ValueError("argv must not contain NUL bytes")

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    @staticmethod
    def _kill_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
