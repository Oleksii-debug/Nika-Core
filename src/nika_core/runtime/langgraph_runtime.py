from __future__ import annotations

import asyncio
import json
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
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)


def _default_resume_command(value: Any) -> Any:
    from langgraph.types import Command

    return Command(resume=value)


def _stable_sort_key(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_safe_value(item) for item in value]
        return sorted(normalized, key=_stable_sort_key)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return repr(value)


def _checkpoint_id(snapshot: Any) -> str | None:
    raw_config = (
        snapshot.get("config")
        if isinstance(snapshot, Mapping)
        else getattr(snapshot, "config", None)
    )
    if not isinstance(raw_config, Mapping):
        return None
    configurable = raw_config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    raw_checkpoint_id = configurable.get("checkpoint_id")
    if raw_checkpoint_id is None:
        return None
    checkpoint_id = str(raw_checkpoint_id).strip()
    return checkpoint_id or None


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


@dataclass(slots=True)
class _ActiveInvocation:
    task_id: str
    task: asyncio.Task[Any]


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
        # LangGraph persists by thread_id. A thread therefore has exactly one in-process owner,
        # even when two Nika tasks accidentally request the same cursor concurrently.
        self._active: dict[str, _ActiveInvocation] = {}

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        """Return the durable cursor Nika can persist before awaiting execution.

        LangGraph checkpoint lookup is keyed by ``thread_id``. Persisting it before the
        invocation starts closes the process-loss window where LangGraph may already have
        durable checkpoints but Nika has not yet received a RuntimeResult. Startup recovery
        must still probe the checkpointer because the process may die before the first
        LangGraph checkpoint is written.
        """
        del task_id
        return thread_id

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        """Verify a LangGraph checkpoint without exposing StateSnapshot/checkpointer types."""
        del task_id
        if resume_token != thread_id:
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.INVALID,
                reason="resume token does not match LangGraph thread_id",
            )

        state_reader = getattr(self._graph, "aget_state", None)
        if not callable(state_reader):
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.UNVERIFIABLE,
                reason="compiled graph does not expose async checkpoint state lookup",
            )

        try:
            snapshot = await state_reader(self._thread_config(thread_id))
        except Exception:  # noqa: BLE001 - checkpoint boundary must fail closed
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.UNREADABLE,
                reason="checkpoint lookup failed",
            )

        checkpoint_id = _checkpoint_id(snapshot)
        if checkpoint_id is None:
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.MISSING,
                reason="no persisted LangGraph checkpoint exists for thread",
            )
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="persisted LangGraph checkpoint is readable",
            checkpoint_id=checkpoint_id,
        )

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

        probe = await self.probe_resume(
            task_id=request.task_id,
            thread_id=request.thread_id,
            resume_token=request.resume_token,
        )
        if not probe.can_resume:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error=f"durable resume blocked: {probe.reason}",
                error_code=RuntimeErrorCode.RESUME_UNAVAILABLE,
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
        active = self._active.get(thread_id)
        if active is None or active.task.done() or active.task_id != task_id:
            return False
        active.task.cancel()
        with suppress(asyncio.CancelledError):
            await active.task
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
        existing = self._active.get(thread_id)
        if existing is not None and not existing.task.done():
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error="runtime execution is already active for LangGraph thread_id",
                error_code=RuntimeErrorCode.DUPLICATE_ACTIVE,
            )

        invocation = asyncio.create_task(self._graph.ainvoke(graph_input, config=config))
        self._active[thread_id] = _ActiveInvocation(task_id=task_id, task=invocation)
        try:
            if timeout_seconds is None:
                raw = await invocation
            else:
                raw = await asyncio.wait_for(invocation, timeout=timeout_seconds)
        except TimeoutError:
            resume_token = await self._resume_token_if_safe(
                task_id=task_id,
                thread_id=thread_id,
            )
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error=f"runtime invocation exceeded {timeout_seconds:g} seconds",
                error_code=RuntimeErrorCode.TIMEOUT,
                resume_token=resume_token,
            )
        except asyncio.CancelledError:
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        except Exception:  # noqa: BLE001 - framework boundary normalizes unknown failures
            resume_token = await self._resume_token_if_safe(
                task_id=task_id,
                thread_id=thread_id,
            )
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error="runtime execution failed",
                error_code=RuntimeErrorCode.INTERNAL,
                resume_token=resume_token,
            )
        finally:
            active = self._active.get(thread_id)
            if active is not None and active.task is invocation:
                self._active.pop(thread_id, None)
        return self._normalize(raw, thread_id=thread_id)

    async def _resume_token_if_safe(self, *, task_id: str, thread_id: str) -> str | None:
        probe = await self.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=thread_id,
        )
        return thread_id if probe.can_resume else None

    @staticmethod
    def _thread_config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

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
