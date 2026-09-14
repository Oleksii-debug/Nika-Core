from __future__ import annotations

import ctypes
import json
import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from nika_core.speech.contracts import (
    SpeechError,
    SpeechErrorCode,
    SpeechReceipt,
    SpeechRequest,
    SpeechVoice,
)

_ENGINE_ID = "windows-system-speech"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_PROCESS_SPEECH_LOCK = threading.Lock()

_LIST_VOICES_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $voices = @(
        $synth.GetInstalledVoices() | ForEach-Object {
            $info = $_.VoiceInfo
            [PSCustomObject]@{
                voice_id = [string]$info.Name
                culture = [string]$info.Culture.Name
                gender = [string]$info.Gender
                age = [string]$info.Age
                enabled = [bool]$_.Enabled
            }
        }
    )
    [Console]::Out.Write((ConvertTo-Json -Compress -InputObject $voices))
}
finally {
    $synth.Dispose()
}
""".strip()

_SPEAK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
Add-Type -AssemblyName System.Speech
$raw = [Console]::In.ReadToEnd()
$payload = ConvertFrom-Json -InputObject $raw
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    if ($null -ne $payload.voice_id -and [string]$payload.voice_id -ne '') {
        $synth.SelectVoice([string]$payload.voice_id)
    }
    $synth.Rate = [int]$payload.rate
    $synth.Volume = [int]$payload.volume
    $synth.SetOutputToDefaultAudioDevice()
    $selectedVoice = [string]$synth.Voice.Name
    $synth.Speak([string]$payload.text)
    [Console]::Out.Write(
        (ConvertTo-Json -Compress -InputObject @{ voice_id = $selectedVoice })
    )
}
finally {
    $synth.Dispose()
}
""".strip()


class WindowsSpeechBackendPort(Protocol):
    def list_voices(self, *, timeout_seconds: float) -> bytes: ...

    def speak(
        self,
        payload: bytes,
        *,
        timeout_seconds: float,
        cancel_event: Event | None,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    stdout: bytes
    returncode: int


class WindowsPowerShellSpeechBackend:
    def __init__(self) -> None:
        if os.name != "nt":
            raise SpeechError(
                SpeechErrorCode.PLATFORM_UNSUPPORTED,
                "Windows System.Speech is available only on Windows",
            )
        windows_dir = _get_windows_directory()
        powershell = (
            windows_dir
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        taskkill = windows_dir / "System32" / "taskkill.exe"
        self._powershell = _validate_trusted_system_executable(
            powershell,
            label="Windows PowerShell speech host",
        )
        self._taskkill = _validate_trusted_system_executable(
            taskkill,
            label="Windows process termination host",
        )

    @classmethod
    def discover(cls) -> WindowsPowerShellSpeechBackend:
        return cls()

    def list_voices(self, *, timeout_seconds: float) -> bytes:
        outcome = self._run(
            script=_LIST_VOICES_SCRIPT,
            stdin_bytes=b"",
            timeout_seconds=timeout_seconds,
            cancel_event=None,
        )
        return outcome.stdout

    def speak(
        self,
        payload: bytes,
        *,
        timeout_seconds: float,
        cancel_event: Event | None,
    ) -> bytes:
        outcome = self._run(
            script=_SPEAK_SCRIPT,
            stdin_bytes=payload,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        return outcome.stdout

    def _run(
        self,
        *,
        script: str,
        stdin_bytes: bytes,
        timeout_seconds: float,
        cancel_event: Event | None,
    ) -> _ProcessOutcome:
        timeout = _validate_timeout(timeout_seconds)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                (
                    str(self._powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=creationflags,
            )
        except OSError:
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                "speech process could not be started",
            ) from None
        result: list[tuple[bytes, None] | Exception] = []

        def communicate() -> None:
            try:
                result.append(process.communicate(input=stdin_bytes))
            except (OSError, ValueError) as exc:  # pragma: no cover - OS/process boundary
                result.append(exc)

        worker = threading.Thread(target=communicate, daemon=True)
        worker.start()
        deadline = time.monotonic() + timeout
        failure: SpeechError | None = None
        while worker.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                failure = SpeechError(
                    SpeechErrorCode.PROCESS_CANCELLED,
                    "speech output was cancelled",
                )
                _terminate_process_tree(process, taskkill_executable=self._taskkill)
                break
            if time.monotonic() >= deadline:
                failure = SpeechError(
                    SpeechErrorCode.PROCESS_TIMEOUT,
                    "speech output exceeded its deadline",
                    retryable=True,
                )
                _terminate_process_tree(process, taskkill_executable=self._taskkill)
                break
            worker.join(timeout=0.02)

        worker.join(timeout=5)
        if worker.is_alive():
            _terminate_process_tree(process, taskkill_executable=self._taskkill)
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                "speech process did not terminate cleanly",
            )
        if failure is not None:
            raise failure
        if not result:
            raise SpeechError(SpeechErrorCode.PROCESS_FAILED, "speech process returned no result")
        if isinstance(result[0], Exception):
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                "speech process communication failed",
            ) from result[0]
        stdout, _stderr = result[0]
        returncode = int(process.returncode or 0)
        if returncode != 0:
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                f"speech engine failed with exit code {returncode}",
            )
        if len(stdout) > 256 * 1024:
            raise SpeechError(
                SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                "speech engine response exceeded the output limit",
            )
        return _ProcessOutcome(stdout=stdout, returncode=returncode)


class WindowsSystemSpeechAdapter:
    def __init__(self, backend: WindowsSpeechBackendPort | None = None) -> None:
        self._backend = backend if backend is not None else WindowsPowerShellSpeechBackend.discover()

    def list_voices(self, *, timeout_seconds: float = 10.0) -> tuple[SpeechVoice, ...]:
        timeout = _validate_timeout(timeout_seconds)
        try:
            payload = self._backend.list_voices(timeout_seconds=timeout)
        except SpeechError:
            raise
        except Exception:  # noqa: BLE001 - backend diagnostics must not escape
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                "speech engine voice enumeration failed",
            ) from None
        if type(payload) is not bytes:
            raise SpeechError(
                SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                "speech engine returned invalid voice metadata",
            )
        try:
            raw = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeechError(
                SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                "speech engine returned invalid voice metadata",
            ) from exc
        if not isinstance(raw, list):
            raise SpeechError(
                SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                "speech engine voice metadata must be an array",
            )
        voices = tuple(_parse_voice(item) for item in raw)
        ids = [voice.voice_id.casefold() for voice in voices]
        if len(ids) != len(set(ids)):
            raise SpeechError(
                SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                "speech engine returned duplicate voice identities",
            )
        return voices

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: Event | None = None,
    ) -> SpeechReceipt:
        if type(request) is not SpeechRequest:
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                "request must be a SpeechRequest",
            )
        timeout = _validate_timeout(timeout_seconds)
        if cancel_event is not None and cancel_event.is_set():
            raise SpeechError(SpeechErrorCode.PROCESS_CANCELLED, "speech output was cancelled")
        if not _PROCESS_SPEECH_LOCK.acquire(blocking=False):
            raise SpeechError(
                SpeechErrorCode.ENGINE_BUSY,
                "another local speech output is already active",
                retryable=True,
            )
        try:
            payload = json.dumps(
                {
                    "text": request.text,
                    "voice_id": request.voice_id,
                    "rate": request.rate,
                    "volume": request.volume,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                response = self._backend.speak(
                    payload,
                    timeout_seconds=timeout,
                    cancel_event=cancel_event,
                )
            except SpeechError:
                raise
            except Exception:  # noqa: BLE001 - backend diagnostics must not escape
                raise SpeechError(
                    SpeechErrorCode.PROCESS_FAILED,
                    "speech engine failed",
                ) from None
            voice_id = _parse_speak_response(response)
            if request.voice_id is not None and voice_id.casefold() != request.voice_id.casefold():
                raise SpeechError(
                    SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                    "speech engine reported a different voice than requested",
                )
            return SpeechReceipt(
                engine_id=_ENGINE_ID,
                voice_id=voice_id,
                character_count=len(request.text),
                rate=request.rate,
                volume=request.volume,
            )
        finally:
            _PROCESS_SPEECH_LOCK.release()


def _parse_voice(value: Any) -> SpeechVoice:
    if not isinstance(value, dict):
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine voice entry must be an object",
        )
    voice_id = value.get("voice_id")
    enabled = value.get("enabled")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine returned an invalid voice identity",
        )
    if type(enabled) is not bool:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine returned an invalid enabled flag",
        )
    return SpeechVoice(
        voice_id=voice_id,
        culture=_optional_string(value.get("culture")),
        gender=_optional_string(value.get("gender")),
        age=_optional_string(value.get("age")),
        enabled=enabled,
    )


def _parse_speak_response(payload: object) -> str:
    if type(payload) is not bytes:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine returned invalid completion metadata",
        )
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine returned invalid completion metadata",
        ) from exc
    if not isinstance(raw, dict):
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine completion metadata must be an object",
        )
    voice_id = raw.get("voice_id")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine returned an invalid completion voice",
        )
    return voice_id


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech engine returned invalid voice metadata",
        )
    return value or None


def _validate_timeout(timeout_seconds: object) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise SpeechError(SpeechErrorCode.INVALID_REQUEST, "speech timeout must be numeric")
    try:
        timeout = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech timeout is outside the supported bound",
        ) from None
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech timeout must be finite, greater than 0, and at most 3600 seconds",
        )
    return timeout


def _get_windows_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise SpeechError(
            SpeechErrorCode.ENGINE_UNAVAILABLE,
            "unable to resolve the trusted Windows directory",
        )
    return Path(buffer.value).resolve(strict=True)


def _validate_trusted_system_executable(executable: Path, *, label: str) -> Path:
    try:
        resolved = executable.resolve(strict=True)
    except OSError:
        raise SpeechError(
            SpeechErrorCode.ENGINE_UNAVAILABLE,
            f"{label} is unavailable",
        ) from None
    if not resolved.is_file():
        raise SpeechError(
            SpeechErrorCode.ENGINE_UNAVAILABLE,
            f"{label} is not a regular file",
        )
    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(resolved))
    if attributes == _INVALID_FILE_ATTRIBUTES:
        raise SpeechError(
            SpeechErrorCode.ENGINE_UNAVAILABLE,
            f"unable to inspect {label}",
        )
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise SpeechError(
            SpeechErrorCode.ENGINE_UNAVAILABLE,
            f"{label} must not be a reparse point",
        )
    return resolved


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    taskkill_executable: Path,
) -> None:
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            (str(taskkill_executable), "/PID", str(process.pid), "/T", "/F"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except OSError:
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
