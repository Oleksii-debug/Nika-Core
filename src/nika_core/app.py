from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.task_queue import TaskQueue


def build_runtime(config: AppConfig) -> tuple[SQLiteStore, AgentRegistry, TaskQueue]:
    store = SQLiteStore(config.database_path)
    store.initialize()
    registry = AgentRegistry(store)
    queue = TaskQueue(store)
    return store, registry, queue


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nika-core",
        description="Inspect the local Nika Core runtime.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show the installed package version without opening the database.",
    )
    args = parser.parse_args(argv)
    if args.version:
        try:
            installed_version = version("nika-core")
        except PackageNotFoundError:
            installed_version = "source checkout (not installed)"
        print(f"Nika Core {installed_version}")
        return 0

    config = AppConfig.from_environment()
    _store, registry, queue = build_runtime(config)
    print(
        f"Nika Core {config.app_version}: "
        f"agents={registry.count}, queued={queue.count_ready}, db={Path(config.database_path)}"
    )
    return 0
