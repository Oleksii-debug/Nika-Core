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
    ) -> ProcessResult:
        normalized = tuple(str(part) for part in argv)
        self._validate_argv(normalized)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        resolved_cwd = cwd.resolve(strict=True)
        if not resolved_cwd.is_dir():
            raise ValueError("cwd must be a directory")

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
            if stdout_reader.exceeded or stderr_reader.exceeded:
                failure = MediaError(
                    MediaErrorCode.OUTPUT_LIMIT,
                    "media subprocess exceeded the configured output limit",
                )
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
        if failure is not None:
            raise failure

        result = ProcessResult(
            argv=normalized,
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
