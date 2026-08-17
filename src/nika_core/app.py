from __future__ import annotations

from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.task_queue import TaskQueue


def build_runtime(config: AppConfig) -> tuple[SQLiteStore, AgentRegistry, TaskQueue]:
    store = SQLiteStore(config.database_path)
    store.initialize()
    registry = AgentRegistry()
    queue = TaskQueue(store)
    return store, registry, queue


def main() -> int:
    config = AppConfig.from_environment()
    _store, registry, queue = build_runtime(config)
    print(
        f"Nika Core {config.app_version}: "
        f"agents={registry.count}, queued={queue.count_ready}, db={Path(config.database_path)}"
    )
    return 0
