from __future__ import annotations

import ctypes
import os
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from math import ceil, isfinite
from threading import Lock
from typing import Protocol

from nika_core.background_life import OwnerPresence
from nika_core.background_runtime import OwnerPresenceObservation
from nika_core.kernel.audit import AuditLog

_DWORD_MODULUS = 1 << 32
_DWORD_MAX = _DWORD_MODULUS - 1
_MAX_SIGNED_64 = (1 << 63) - 1
_MAX_UNSIGNED_64 = (1 << 64) - 1
_MAX_IDENTITY_LENGTH = 256
_SOURCE_ENTITY_TYPE = "owner_presence_source"
_SAMPLED_EVENT = "background.owner_presence_sampled"
_DEFAULT_SOURCE_ID = "win32-owner-presence"


class WindowsLastInputApi(Protocol):
    """Minimal Win32 input-idle API used by the physical presence adapter."""

    def get_last_input_tick_ms(self) -> int: ...

    def get_tick_count64_ms(self) -> int: ...


class _LastInputInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint32),
    ]


class Win32LastInputApi:
    """Thin ctypes adapter over GetLastInputInfo and GetTickCount64."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 owner-presence probing is available only on Windows")
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("ctypes WinDLL support is unavailable")

        user32 = win_dll("user32", use_last_error=True)
        kernel32 = win_dll("kernel32", use_last_error=True)

        get_last_input_info = user32.GetLastInputInfo
        get_last_input_info.argtypes = [ctypes.POINTER(_LastInputInfo)]
        get_last_input_info.restype = ctypes.c_int

        get_tick_count64 = kernel32.GetTickCount64
        get_tick_count64.argtypes = []
        get_tick_count64.restype = ctypes.c_ulonglong

        self._get_last_input_info = get_last_input_info
        self._get_tick_count64 = get_tick_count64

    def get_last_input_tick_ms(self) -> int:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if self._get_last_input_info(ctypes.byref(info)) == 0:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "GetLastInputInfo failed")
        return int(info.dwTime)

    def get_tick_count64_ms(self) -> int:
        return int(self._get_tick_count64())


class WindowsOwnerPresenceObserver:
    """Physical Windows owner-presence source for the guarded background runtime.

    The adapter observes only aggregate keyboard/mouse inactivity. It never captures
    key contents, pointer coordinates, window titles, process identities or raw input
    events. A successful sample allocates its durable monotonic sequence from the
    canonical audit_events rowid before returning the #855 observation carrier.
    """

    def __init__(
        self,
        audit: AuditLog,
        *,
        away_after_seconds: float,
        source_id: str = _DEFAULT_SOURCE_ID,
        api: WindowsLastInputApi | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(source_id) is not str:
            raise TypeError("source_id must be exact built-in str")
        if not source_id or source_id != source_id.strip():
            raise ValueError("source_id must be non-empty without surrounding whitespace")
        if len(source_id) > _MAX_IDENTITY_LENGTH:
            raise ValueError("source_id is too long")
        if any(unicodedata.category(char).startswith("C") for char in source_id):
            raise ValueError("source_id must not contain control or format characters")
        if type(away_after_seconds) not in (int, float):
            raise TypeError("away_after_seconds must be exact built-in int or float")
        if type(away_after_seconds) is float and not isfinite(away_after_seconds):
            raise ValueError("away_after_seconds must be finite")
        if away_after_seconds <= 0:
            raise ValueError("away_after_seconds must be greater than zero")
        if away_after_seconds >= _DWORD_MODULUS / 1000:
            raise ValueError(
                "away_after_seconds must fit inside the wrap-safe Win32 DWORD idle interval"
            )

        threshold_ms = ceil(away_after_seconds * 1000)
        if not 1 <= threshold_ms < _DWORD_MODULUS:
            raise ValueError(
                "away_after_seconds must fit inside the wrap-safe Win32 DWORD idle interval"
            )

        self._audit = audit
        self._source_id = source_id
        self._threshold_ms = threshold_ms
        self._api = api if api is not None else Win32LastInputApi()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sample_lock = Lock()
        self._previous_current_tick: int | None = None
        self._stable_last_input_tick: int | None = None
        self._stable_since_current_tick: int | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def away_after_seconds(self) -> float:
        return self._threshold_ms / 1000.0

    def observe(self) -> OwnerPresenceObservation:
        with self._sample_lock:
            try:
                return self._observe_locked()
            except Exception:
                self._reset_stability()
                raise

    def _observe_locked(self) -> OwnerPresenceObservation:
        last_before = self._validated_last_input_tick(
            self._api.get_last_input_tick_ms(),
            field_name="last input tick before sample",
        )
        current_tick = self._validated_current_tick(self._api.get_tick_count64_ms())
        last_after = self._validated_last_input_tick(
            self._api.get_last_input_tick_ms(),
            field_name="last input tick after sample",
        )

        previous_current_tick = self._previous_current_tick
        if previous_current_tick is not None and current_tick < previous_current_tick:
            raise ValueError("GetTickCount64 regressed between owner-presence samples")

        next_stable_last_input_tick = self._stable_last_input_tick
        next_stable_since_current_tick = self._stable_since_current_tick
        if last_after != last_before:
            presence = OwnerPresence.ACTIVE
            next_stable_last_input_tick = None
            next_stable_since_current_tick = None
        else:
            if current_tick < _DWORD_MODULUS and last_after > current_tick:
                raise ValueError(
                    "last input tick cannot be ahead of current tick before the first DWORD wrap"
                )
            idle_ms = ((current_tick & _DWORD_MAX) - last_after) & _DWORD_MAX
            if (
                next_stable_last_input_tick != last_after
                or next_stable_since_current_tick is None
            ):
                next_stable_last_input_tick = last_after
                next_stable_since_current_tick = current_tick
                stable_elapsed_ms = 0
            else:
                stable_elapsed_ms = current_tick - next_stable_since_current_tick
            presence = (
                OwnerPresence.AWAY
                if idle_ms >= self._threshold_ms
                and stable_elapsed_ms >= self._threshold_ms
                else OwnerPresence.ACTIVE
            )

        observed_at = self._validated_clock_sample(self._clock())
        sequence = self._audit.append(
            event_type=_SAMPLED_EVENT,
            entity_type=_SOURCE_ENTITY_TYPE,
            entity_id=self._source_id,
            payload={
                "presence": presence.value,
                "probe": "win32_last_input",
                "threshold_ms": self._threshold_ms,
            },
        )
        if type(sequence) is not int or not 0 <= sequence <= _MAX_SIGNED_64:
            raise RuntimeError("audit event id is outside the supported sequence range")

        self._previous_current_tick = current_tick
        self._stable_last_input_tick = next_stable_last_input_tick
        self._stable_since_current_tick = next_stable_since_current_tick
        return OwnerPresenceObservation(
            source_id=self._source_id,
            sequence=sequence,
            presence=presence,
            observed_at=observed_at,
        )

    def _reset_stability(self) -> None:
        self._previous_current_tick = None
        self._stable_last_input_tick = None
        self._stable_since_current_tick = None

    @staticmethod
    def _validated_last_input_tick(value: object, *, field_name: str) -> int:
        if type(value) is not int:
            raise TypeError(f"{field_name} must be exact built-in int")
        if not 0 <= value <= _DWORD_MAX:
            raise ValueError(f"{field_name} is outside the Win32 DWORD range")
        return value

    @staticmethod
    def _validated_current_tick(value: object) -> int:
        if type(value) is not int:
            raise TypeError("current tick must be exact built-in int")
        if not 0 <= value <= _MAX_UNSIGNED_64:
            raise ValueError("current tick is outside the supported integer range")
        return value

    @staticmethod
    def _validated_clock_sample(value: object) -> datetime:
        if type(value) is not datetime:
            raise TypeError("presence clock must return exact built-in datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("presence clock must return timezone-aware datetime")
        if value.utcoffset().total_seconds() != 0:
            raise ValueError("presence clock must use UTC")
        return value
