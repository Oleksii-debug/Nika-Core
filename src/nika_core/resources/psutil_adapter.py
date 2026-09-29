from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import psutil

from nika_core.resources.contracts import (
    ResourceObserverPort,
    ResourceOwnerProbePort,
    ResourceProcessIdentity,
    ResourceSnapshot,
)


class PsutilResourceObserver(ResourceObserverPort, ResourceOwnerProbePort):
    """Cross-platform read-only telemetry plus restart owner-generation proof."""

    def __init__(
        self,
        process: Any | None = None,
        *,
        disk_path: Path | str | None = None,
    ) -> None:
        self._process = process if process is not None else psutil.Process()
        self._disk_path = (
            Path(disk_path)
            if disk_path is not None
            else (Path.cwd() if process is None else None)
        )

    def snapshot(self) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        disk_percent: float | None = None
        available_disk_bytes: int | None = None
        if self._disk_path is not None:
            disk = psutil.disk_usage(str(self._disk_path))
            disk_percent = float(disk.percent)
            available_disk_bytes = int(disk.free)

        process_rss_bytes: int | None
        try:
            process_rss_bytes = int(self._process.memory_info().rss)
        except (AttributeError, OSError, psutil.Error):
            process_rss_bytes = None

        battery_percent: float | None = None
        power_plugged: bool | None = None
        try:
            battery = psutil.sensors_battery()
        except (AttributeError, NotImplementedError, OSError, psutil.Error):
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
            disk_percent=disk_percent,
            available_disk_bytes=available_disk_bytes,
            process_rss_bytes=process_rss_bytes,
            gpu_percent=None,
            battery_percent=battery_percent,
            power_plugged=power_plugged,
        )

    def current_process_identity(self) -> ResourceProcessIdentity:
        return ResourceProcessIdentity(
            process_id=int(self._process.pid),
            started_at=float(self._process.create_time()),
        )

    def is_process_alive(self, identity: ResourceProcessIdentity) -> bool:
        try:
            process = psutil.Process(identity.process_id)
            started_at = float(process.create_time())
            if not math.isclose(started_at, identity.started_at, rel_tol=0.0, abs_tol=1e-6):
                return False
            if not process.is_running():
                return False
            return process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except psutil.AccessDenied:
            return True
