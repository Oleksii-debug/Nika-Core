from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry
from nika_core.runtime.contracts import AgentRuntimePort, RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.reference import ReferenceRuntime
from nika_core.ui.bridge_models import UIResult

_DEFAULT_AGENT_ID = "nika.default"
_DEFAULT_WORKSPACE_ID = "default"
_TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.ARCHIVED}
)
_LOCAL_CANCEL_STATES = frozenset(
    {TaskState.CREATED, TaskState.READY, TaskState.PAUSED, TaskState.BLOCKED}
)


@dataclass(frozen=True, slots=True)
class _LiveRun:
    thread_id: str
    future: Future[Any]


class _BackgroundRuntimeLoop:
    """One event loop for live desktop runtime effects and their controls."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="nika-desktop-runtime",
            daemon=True,
        )
        self._thread.start()
        self._started.wait()

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("desktop runtime loop is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()


class DesktopBackend:
    """Small product-facing facade for the packaged Windows shell.

    Runtime work is dispatched on one background asyncio loop so the WebView bridge remains
    responsive while work is RUNNING. Runtime control calls use that same loop. This facade
    never invents a pause side effect: a RUNNING runtime can only become PAUSED when the
    runtime/coordinator reports that outcome through the canonical runtime contract.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue,
        agents: AgentRegistry,
        workspaces: WorkspaceRegistry,
        audit: AuditLog,
        runtime: AgentRuntimePort | None = None,
    ) -> None:
        self._queue = queue
        self._agents = agents
        self._workspaces = workspaces
        self._audit = audit
        self._coordinator = TaskRuntimeCoordinator(queue, audit)
        self._runtime = runtime or ReferenceRuntime()
        self._activity_lock = threading.Lock()
        self._background: _BackgroundRuntimeLoop | None = None
        self._live_runs: dict[str, _LiveRun] = {}
        self._cancel_futures: dict[str, Future[Any]] = {}
        self._ensure_defaults()

    def create_task(self, payload: Mapping[str, Any]) -> UIResult:
        command = str(payload.get("command", "")).strip()
        if not command:
            raise ValueError("Введіть команду перед створенням завдання.")
        record = self._queue.create(
            workspace_id=_DEFAULT_WORKSPACE_ID,
            agent_id=_DEFAULT_AGENT_ID,
            payload={"command": command},
        )
        self._queue.transition(record.task_id, TaskState.READY)
        thread_id = f"desktop-{uuid.uuid4()}"
        self._submit_run(
            task_id=record.task_id,
            thread_id=thread_id,
            coroutine=self._coordinator.start(
                self._runtime,
                RuntimeRequest(
                    task_id=record.task_id,
                    thread_id=thread_id,
                    payload={"command": command},
                ),
            ),
        )
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message=f"Завдання прийнято до виконання: {command}",
            focus_id="tasks-heading",
        )

    def pause_task(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._latest_controllable()
        if record is None:
            raise ValueError("Немає активного завдання, яке можна призупинити.")
        if record.state is TaskState.RUNNING:
            raise ValueError(
                "Поточний runtime не підтримує зовнішній pause-контракт. "
                "Скористайтеся зупинкою або дочекайтеся стану PAUSED від runtime."
            )
        if record.state is not TaskState.READY:
            raise ValueError(f"Завдання у стані {record.state.value} не можна призупинити.")
        if self._live_run(record.task_id) is not None:
            raise ValueError(
                "Завдання вже передано runtime; безпечний pause до початку виконання недоступний."
            )
        self._queue.transition(record.task_id, TaskState.PAUSED)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Завдання призупинено до запуску runtime.",
            focus_id="tasks-heading",
        )

    def resume_task(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._latest_with_state(TaskState.PAUSED)
        if record is None:
            raise ValueError("Немає призупиненого завдання для продовження.")
        session = self._coordinator.sessions.get(record.task_id)
        if session is None:
            if self._has_started(record.task_id):
                raise ValueError(
                    "Призупинене завдання вже входило в RUNNING, але не має збереженої "
                    "runtime-сесії; повторний старт відхилено, щоб не дублювати побічні ефекти."
                )
            self._queue.transition(record.task_id, TaskState.READY)
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="Завдання повернуто до черги виконання.",
                focus_id="tasks-heading",
            )
        if session.runtime_id != self._runtime.runtime_id:
            raise ValueError(
                "Збережена runtime-сесія належить іншому runtime; безпечне продовження відхилено."
            )
        self._submit_run(
            task_id=record.task_id,
            thread_id=session.thread_id,
            coroutine=self._coordinator.resume_saved(
                self._runtime,
                task_id=record.task_id,
            ),
        )
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Запит на продовження передано runtime.",
            focus_id="tasks-heading",
        )

    def stop_agent(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._latest_controllable()
        if record is None:
            raise ValueError("Немає активного завдання агента для зупинки.")

        live = self._live_run(record.task_id)
        session = self._coordinator.sessions.get(record.task_id)
        if live is not None:
            return self._request_runtime_cancel(
                record.task_id,
                thread_id=live.thread_id,
            )
        if session is not None:
            if session.runtime_id != self._runtime.runtime_id:
                raise ValueError(
                    "Активна runtime-сесія належить іншому runtime; безпечне скасування відхилено."
                )
            return self._request_runtime_cancel(
                record.task_id,
                thread_id=session.thread_id,
            )
        if record.state in _LOCAL_CANCEL_STATES:
            self._queue.transition(record.task_id, TaskState.CANCELLED)
        else:
            raise ValueError(
                "Неможливо безпечно скасувати активне runtime-завдання без збереженої "
                "або поточної runtime-сесії."
            )

        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Поточне завдання агента скасовано.",
            focus_id="tasks-heading",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "tasks": [self._task_view(record) for record in self._queue.list_recent(limit=50)],
            "agents": [
                {
                    "agent_id": item.agent_id,
                    "name": item.name,
                    "version": item.version,
                    "goal": item.goal,
                }
                for item in self._agents.list_latest()
            ],
            "workspaces": [
                {
                    "workspace_id": item.workspace_id,
                    "name": item.name,
                    "version": item.version,
                    "description": item.description,
                    "enabled": item.enabled,
                }
                for item in self._workspaces.list_latest()
            ],
        }

    def close(self) -> None:
        """Release the private runtime loop; intended for deterministic host/test teardown."""
        with self._activity_lock:
            background = self._background
            self._background = None
        if background is not None:
            background.close()

    def _submit_run(
        self,
        *,
        task_id: str,
        thread_id: str,
        coroutine: Coroutine[Any, Any, Any],
    ) -> None:
        background = self._runtime_loop()
        with self._activity_lock:
            existing = self._live_runs.get(task_id)
            if existing is not None and not existing.future.done():
                coroutine.close()
                raise ValueError("Завдання вже має активне runtime-виконання.")
            try:
                future = background.submit(coroutine)
            except Exception:
                coroutine.close()
                raise
            self._live_runs[task_id] = _LiveRun(thread_id=thread_id, future=future)
        future.add_done_callback(
            lambda done, current_task_id=task_id: self._run_finished(
                current_task_id,
                done,
            )
        )

    def _request_runtime_cancel(self, task_id: str, *, thread_id: str) -> UIResult:
        background = self._runtime_loop()
        with self._activity_lock:
            existing = self._cancel_futures.get(task_id)
            if existing is not None and not existing.done():
                raise ValueError("Скасування цього завдання вже очікує підтвердження runtime.")
            future = background.submit(
                self._coordinator.cancel(
                    self._runtime,
                    task_id=task_id,
                    thread_id=thread_id,
                )
            )
            self._cancel_futures[task_id] = future
        future.add_done_callback(
            lambda done, current_task_id=task_id: self._cancel_finished(
                current_task_id,
                done,
            )
        )
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Запит на зупинку передано runtime; стан оновиться після підтвердження.",
            focus_id="tasks-heading",
        )

    def _run_finished(self, task_id: str, future: Future[Any]) -> None:
        with self._activity_lock:
            current = self._live_runs.get(task_id)
            if current is not None and current.future is future:
                self._live_runs.pop(task_id, None)
        self._observe_background_failure(
            task_id=task_id,
            operation="run",
            future=future,
        )

    def _cancel_finished(self, task_id: str, future: Future[Any]) -> None:
        with self._activity_lock:
            if self._cancel_futures.get(task_id) is future:
                self._cancel_futures.pop(task_id, None)
        self._observe_background_failure(
            task_id=task_id,
            operation="cancel",
            future=future,
        )

    def _observe_background_failure(
        self,
        *,
        task_id: str,
        operation: str,
        future: Future[Any],
    ) -> None:
        if future.cancelled():
            return
        exception = future.exception()
        if exception is None:
            return
        self._audit.append(
            event_type="desktop.runtime_background_failed",
            entity_type="task",
            entity_id=task_id,
            payload={
                "operation": operation,
                "error_type": type(exception).__name__,
            },
        )

    def _runtime_loop(self) -> _BackgroundRuntimeLoop:
        with self._activity_lock:
            if self._background is None:
                self._background = _BackgroundRuntimeLoop()
            return self._background

    def _live_run(self, task_id: str) -> _LiveRun | None:
        with self._activity_lock:
            live = self._live_runs.get(task_id)
            if live is None or live.future.done():
                return None
            return live

    def _has_started(self, task_id: str) -> bool:
        with self._queue.store.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND new_state = ? LIMIT 1",
                (task_id, TaskState.RUNNING.value),
            ).fetchone()
        return row is not None

    def _ensure_defaults(self) -> None:
        try:
            self._agents.get(_DEFAULT_AGENT_ID)
        except KeyError:
            self._agents.register(
                AgentDefinition(
                    agent_id=_DEFAULT_AGENT_ID,
                    name="Nika",
                    version=1,
                    goal="Виконувати локальні безпечні завдання через контрольований runtime.",
                )
            )
        try:
            self._workspaces.get(_DEFAULT_WORKSPACE_ID)
        except KeyError:
            self._workspaces.register(
                WorkspaceDefinition(
                    workspace_id=_DEFAULT_WORKSPACE_ID,
                    name="Основний",
                    version=1,
                    description="Основний локальний робочий простір Nika Core.",
                )
            )

    def _latest_controllable(self) -> TaskRecord | None:
        for record in self._queue.list_recent(limit=50):
            if record.state not in _TERMINAL_STATES:
                return record
        return None

    def _latest_with_state(self, state: TaskState) -> TaskRecord | None:
        for record in self._queue.list_recent(limit=50):
            if record.state == state:
                return record
        return None

    @staticmethod
    def _task_view(record: TaskRecord) -> dict[str, Any]:
        command = str(record.payload.get("command", "")).strip()
        return {
            "task_id": record.task_id,
            "workspace_id": record.workspace_id,
            "agent_id": record.agent_id,
            "state": record.state.value,
            "command": command,
        }
