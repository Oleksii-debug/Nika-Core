from __future__ import annotations

import math
from pathlib import Path

import psutil

from nika_core.resources.contracts import (
    ResourceObserverPort,
    ResourceOwnerProbePort,
    ResourceProcessIdentity,
    ResourceSnapshot,
)


class PsutilResourceObserver(ResourceObserverPort, ResourceOwnerProbePort):
    def __init__(self, *, disk_path: Path | str | None = None) -> None:
        self._disk_path = Path(disk_path) if disk_path is not None else Path.cwd()
        self._process = psutil.Process()
        self._first_snapshot = True

    def snapshot(self) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(self._disk_path)
        process_memory = self._process.memory_info()
        interval = 0.1 if self._first_snapshot else None
        cpu_percent = float(psutil.cpu_percent(interval=interval))
        self._first_snapshot = False
        return ResourceSnapshot(
            cpu_percent=cpu_percent,
            memory_percent=float(memory.percent),
            available_memory_bytes=int(memory.available),
            disk_percent=float(disk.percent),
            available_disk_bytes=int(disk.free),
            process_rss_bytes=int(process_memory.rss),
            gpu_percent=None,
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
