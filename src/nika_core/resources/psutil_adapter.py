from __future__ import annotations

import psutil

from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot


class PsutilResourceObserver(ResourceObserverPort):
    def snapshot(self) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        return ResourceSnapshot(
            cpu_percent=float(psutil.cpu_percent(interval=None)),
            memory_percent=float(memory.percent),
            available_memory_bytes=int(memory.available),
        )
