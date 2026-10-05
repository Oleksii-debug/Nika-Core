from __future__ import annotations

import math
from typing import Any

import psutil

from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot


def _optional_nonnegative_int(value: object) -> int | None:
    """Do not turn malformed optional psutil measurements into invented capacity."""
    return value if type(value) is int and value >= 0 else None


def _optional_battery_percent(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        percent = float(value)
    except (OverflowError, ValueError):
        return None
    return percent if math.isfinite(percent) and 0 <= percent <= 100 else None


def _required_host_percent(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite percentage")
    try:
        percent = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite percentage") from None
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError(f"{name} must be a finite percentage")
    return percent


def _required_available_bytes(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Host available memory must be a nonnegative integer")
    return value


class PsutilResourceObserver(ResourceObserverPort):
    """Cross-platform read-only host/process telemetry behind the Nika resource port."""

    def __init__(self, process: Any | None = None) -> None:
        self._process = process if process is not None else psutil.Process()

    def snapshot(self) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        process_rss_bytes: int | None
        try:
            process_rss_bytes = _optional_nonnegative_int(self._process.memory_info().rss)
        except (AttributeError, OSError, psutil.Error):
            process_rss_bytes = None

        battery_percent: float | None = None
        power_plugged: bool | None = None
        try:
            battery = psutil.sensors_battery()
        except (AttributeError, NotImplementedError, OSError, psutil.Error):
            battery = None
        if battery is not None:
            battery_percent = _optional_battery_percent(battery.percent)
            # None means "unknown", not "running on battery". Reject truthy substitutes.
            power_plugged = (
                battery.power_plugged if type(battery.power_plugged) is bool else None
            )

        try:
            logical_cpu_count = psutil.cpu_count(logical=True)
        except (AttributeError, OSError, psutil.Error):
            logical_cpu_count = None
        if type(logical_cpu_count) is not int or logical_cpu_count <= 0:
            logical_cpu_count = None
        # Validate the raw psutil values before conversion can launder bools or strings.
        cpu_percent = _required_host_percent(psutil.cpu_percent(interval=None), "Host CPU")
        memory_percent = _required_host_percent(memory.percent, "Host memory")
        available_memory_bytes = _required_available_bytes(memory.available)
        return ResourceSnapshot(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            available_memory_bytes=available_memory_bytes,
            logical_cpu_count=logical_cpu_count,
            total_memory_bytes=_optional_nonnegative_int(memory.total),
            process_rss_bytes=process_rss_bytes,
            battery_percent=battery_percent,
            power_plugged=power_plugged,
        )
