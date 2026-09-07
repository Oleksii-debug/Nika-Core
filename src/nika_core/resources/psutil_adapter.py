from __future__ import annotations

from typing import Any

import psutil

from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot


class PsutilResourceObserver(ResourceObserverPort):
    """Cross-platform read-only host/process telemetry behind the Nika resource port."""

    def __init__(self, process: Any | None = None) -> None:
        self._process = process if process is not None else psutil.Process()

    def snapshot(self) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        process_rss_bytes: int | None
        try:
            process_rss_bytes = int(self._process.memory_info().rss)
        except (AttributeError, OSError, psutil.Error):
            process_rss_bytes = None

        battery_percent: float | None = None
        power_plugged: bool | None = None
        try:
            battery = psutil.sensors_battery()
        except (AttributeError, NotImplementedError, OSError):
            battery = None
        if battery is not None:
            battery_percent = float(battery.percent)
            power_plugged = bool(battery.power_plugged)

        logical_cpu_count = psutil.cpu_count(logical=True)
        return ResourceSnapshot(
            cpu_percent=float(psutil.cpu_percent(interval=None)),
            memory_percent=float(memory.percent),
            available_memory_bytes=int(memory.available),
            logical_cpu_count=(
                int(logical_cpu_count) if logical_cpu_count is not None else None
            ),
            total_memory_bytes=int(memory.total),
            process_rss_bytes=process_rss_bytes,
            battery_percent=battery_percent,
            power_plugged=power_plugged,
        )
