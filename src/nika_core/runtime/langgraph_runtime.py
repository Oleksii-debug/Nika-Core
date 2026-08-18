from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeErrorCode,
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


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in value]
    return repr(value)


@dataclass(slots=True)
class LangGraphSqliteHandle:
    """Owned async SQLite/checkpointer pair used by LangGraphRuntime graphs."""

    connection: Any
    checkpointer: Any
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.connection.close()


@asynccontextmanager
async def open_langgraph_sqlite(path: Path) -> AsyncIterator[LangGraphSqliteHandle]:
    """Open a durable async SQLite checkpointer with strict deserialization enabled.

    LangGraphRuntime uses ``graph.ainvoke``. The synchronous ``SqliteSaver`` deliberately
    does not implement the asynchronous checkpoint API, so the runtime boundary must use
    ``AsyncSqliteSaver`` and own/close its aiosqlite connection explicitly.
    """

    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(path)
    try:
        checkpointer = AsyncSqliteSaver(connection)
        await checkpointer.setup()
    except Exception:
        await connection.close()
        raise

    handle = LangGraphSqliteHandle(connection=connection, checkpointer=checkpointer)
    try:
        yield handle
    finally:
        await handle.close()


class LangGraphRuntime:
    runtime_id = "langgraph"
    capabilities = frozenset(
        {
            RuntimeCapability.DURABLE_RESUME,
            RuntimeCapability.HUMAN_APPROVAL,
            RuntimeCapability.CANCELLATION,
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
        self._active: dict[tuple[str, str], asyncio.Task[Any]] = {}

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        config = self._config(request.thread_id, request.max_steps)
        return await self._execute(
            task_id=request.task_id,
            thread_id=request.thread_id,
            graph_input=dict(request.payload),
            config=config,
            timeout_seconds=request.timeout_seconds,
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        if request.resume_token != request.thread_id:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error="resume token does not match LangGraph thread_id",
                error_code=RuntimeErrorCode.INVALID_RESUME,
            )

        config = self._config(request.thread_id, request.max_steps)
        graph_input = (
            None
            if request.mode == RuntimeResumeMode.CONTINUE
            else self._resume_command_factory(request.value)
        )
        return await self._execute(
            task_id=request.task_id,
            thread_id=request.thread_id,
            graph_input=graph_input,
            config=config,
            timeout_seconds=request.timeout_seconds,
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        key = (task_id, thread_id)
        active = self._active.get(key)
        if active is None or active.done():
            return False
        active.cancel()
        with suppress(asyncio.CancelledError):
            await active
        return True

    async def _execute(
        self,
        *,
        task_id: str,
        thread_id: str,
        graph_input: Any,
        config: Mapping[str, Any],
        timeout_seconds: float | None,
    ) -> RuntimeResult:
        key = (task_id, thread_id)
        existing = self._active.get(key)
        if existing is not None and not existing.done():
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error="runtime execution is already active for task/thread",
                error_code=RuntimeErrorCode.DUPLICATE_ACTIVE,
            )

        invocation = asyncio.create_task(self._graph.ainvoke(graph_input, config=config))
        self._active[key] = invocation
        try:
            if timeout_seconds is None:
                raw = await invocation
            else:
                raw = await asyncio.wait_for(invocation, timeout=timeout_seconds)
        except TimeoutError:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error=f"runtime invocation exceeded {timeout_seconds:g} seconds",
                error_code=RuntimeErrorCode.TIMEOUT,
                resume_token=thread_id,
            )
        except asyncio.CancelledError:
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        except Exception as exc:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error=str(exc),
                error_code=RuntimeErrorCode.INTERNAL,
                resume_token=thread_id,
            )
        finally:
            if self._active.get(key) is invocation:
                self._active.pop(key, None)
        return self._normalize(raw, thread_id=thread_id)

    @staticmethod
    def _config(thread_id: str, max_steps: int) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max_steps,
        }

    @staticmethod
    def _normalize(raw: Any, *, thread_id: str) -> RuntimeResult:
        if not isinstance(raw, Mapping):
            return RuntimeResult(
                outcome=RuntimeOutcome.COMPLETED,
                output={"value": _safe_value(raw)},
            )

        interrupts = tuple(raw.get("__interrupt__", ()) or ())
        output = {
            str(key): _safe_value(value)
            for key, value in raw.items()
            if key != "__interrupt__"
        }
        if interrupts:
            events = tuple(
                RuntimeEvent(
                    sequence=index,
                    event_type="runtime.approval_requested",
                    payload={"value": _safe_value(getattr(item, "value", repr(item)))},
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
