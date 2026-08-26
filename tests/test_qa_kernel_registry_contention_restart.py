from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry


RegisterCall = Callable[[], None]


def _run_together(calls: list[RegisterCall]) -> list[Future[None]]:
    barrier = Barrier(len(calls))

    def invoke(call: RegisterCall) -> None:
        barrier.wait(timeout=5)
        call()

    executor = ThreadPoolExecutor(max_workers=len(calls))
    try:
        futures = [executor.submit(invoke, call) for call in calls]
        for future in futures:
            try:
                future.result(timeout=15)
            except ValueError:
                continue
            except sqlite3.Error as exc:
                pytest.fail(f"registry leaked sqlite error under contention: {exc}")
        return futures
    finally:
        executor.shutdown(wait=True)


def _count_successes(futures: list[Future[None]]) -> int:
    successes = 0
    for future in futures:
        try:
            future.result()
        except ValueError:
            continue
        successes += 1
    return successes


def test_agent_registry_duplicate_contention_is_contract_safe_and_restart_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent registry contention.db"
    store = SQLiteStore(database)
    store.initialize()
    AgentRegistry(store).register(AgentDefinition("worker", "Worker", 1, "Initial"))

    definition = AgentDefinition("worker", "Worker", 2, "Updated")
    calls = [
        (lambda registry=AgentRegistry(store): registry.register(definition))
        for _ in range(8)
    ]

    futures = _run_together(calls)

    assert _count_successes(futures) == 1
    reopened = AgentRegistry(SQLiteStore(database))
    assert reopened.get("worker") == definition


def test_workspace_registry_duplicate_contention_is_contract_safe_and_restart_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workspace registry contention.db"
    store = SQLiteStore(database)
    store.initialize()
    WorkspaceRegistry(store).register(WorkspaceDefinition("research", "Research", 1))

    definition = WorkspaceDefinition("research", "Research", 2, "Updated")
    calls = [
        (lambda registry=WorkspaceRegistry(store): registry.register(definition))
        for _ in range(8)
    ]

    futures = _run_together(calls)

    assert _count_successes(futures) == 1
    reopened = WorkspaceRegistry(SQLiteStore(database))
    assert reopened.get("research") == definition


def test_agent_registry_mixed_versions_converge_to_highest_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent mixed versions.db"
    store = SQLiteStore(database)
    store.initialize()
    AgentRegistry(store).register(AgentDefinition("worker", "Worker", 1, "Initial"))

    definitions = [
        AgentDefinition("worker", f"Worker v{version}", version, f"Goal v{version}")
        for version in (2, 7, 3, 6, 4, 5)
    ]
    calls = [
        (
            lambda definition=definition, registry=AgentRegistry(store): registry.register(
                definition
            )
        )
        for definition in definitions
    ]

    _run_together(calls)

    reopened = AgentRegistry(SQLiteStore(database))
    current = reopened.get("worker")
    assert current.version == 7
    assert current.name == "Worker v7"
    assert current.goal == "Goal v7"


def test_workspace_registry_mixed_versions_converge_to_highest_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workspace mixed versions.db"
    store = SQLiteStore(database)
    store.initialize()
    WorkspaceRegistry(store).register(WorkspaceDefinition("research", "Research", 1))

    definitions = [
        WorkspaceDefinition(
            "research",
            f"Research v{version}",
            version,
            f"Description v{version}",
        )
        for version in (2, 7, 3, 6, 4, 5)
    ]
    calls = [
        (
            lambda definition=definition, registry=WorkspaceRegistry(store): registry.register(
                definition
            )
        )
        for definition in definitions
    ]

    _run_together(calls)

    reopened = WorkspaceRegistry(SQLiteStore(database))
    current = reopened.get("research")
    assert current.version == 7
    assert current.name == "Research v7"
    assert current.description == "Description v7"
