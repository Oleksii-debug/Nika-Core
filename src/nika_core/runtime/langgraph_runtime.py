from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)


def _default_resume_command(value: Any) -> Any:
    from langgraph.types import Command

    return Command(resume=value)


@dataclass(slots=True)
class LangGraphSqliteHandle:
    connection: sqlite3.Connection
    checkpointer: Any

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> LangGraphSqliteHandle:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def open_langgraph_sqlite(path: Path) -> LangGraphSqliteHandle:
    """Open the local LangGraph SQLite checkpointer with strict deserialization enabled."""

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    from langgraph.checkpoint.sqlite import SqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        checkpointer = SqliteSaver(connection)
        checkpointer.setup()
    except Exception:
        connection.close()
        raise
    return LangGraphSqliteHandle(connection, checkpointer)


class LangGraphRuntime:
    runtime_id = "langgraph"
    capabilities = frozenset(
        {
            RuntimeCapability.DURABLE_RESUME,
            RuntimeCapability.HUMAN_APPROVAL,
            RuntimeCapability.PARALLELISM,
        }
    )

    def __init__(
        self,
        graph: Any,
        *,
        resume_command_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if not callable(getattr(graph, "ainvoke", None)):
            raise TypeError("graph must provide an async ainvoke method")
        self._graph = graph
        self._resume_command_factory = resume_command_factory or _default_resume_command

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        config = self._config(request.thread_id)
        try:
            raw = await self._graph.ainvoke(dict(request.payload), config=config)
        except Exception as exc:
            return RuntimeResult(outcome=RuntimeOutcome.FAILED, error=str(exc))
        return self._normalize(raw, thread_id=request.thread_id)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        config = self._config(request.thread_id)
        graph_input = (
            None
            if request.mode == RuntimeResumeMode.CONTINUE
            else self._resume_command_factory(request.value)
        )
        try:
            raw = await self._graph.ainvoke(graph_input, config=config)
        except Exception as exc:
            return RuntimeResult(outcome=RuntimeOutcome.FAILED, error=str(exc))
        return self._normalize(raw, thread_id=request.thread_id)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        # Cancellation is intentionally not advertised until a real behavior proof exists.
        return False

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _normalize(raw: Any, *, thread_id: str) -> RuntimeResult:
        if not isinstance(raw, Mapping):
            return RuntimeResult(
                outcome=RuntimeOutcome.COMPLETED,
                output={"value": raw},
            )

        interrupts = tuple(raw.get("__interrupt__", ()) or ())
        output = {key: value for key, value in raw.items() if key != "__interrupt__"}
        if interrupts:
            events = tuple(
                RuntimeEvent(
                    sequence=index,
                    event_type="runtime.approval_requested",
                    payload={"value": getattr(item, "value", repr(item))},
                )
                for index, item in enumerate(interrupts)
            )
            return RuntimeResult(
                outcome=RuntimeOutcome.WAITING_APPROVAL,
                events=events,
                output=output,
                resume_token=thread_id,
            )
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output=output)
