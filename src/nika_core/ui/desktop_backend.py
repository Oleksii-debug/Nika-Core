from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine, Mapping
from concurrent.futures import Future
from typing import Any

from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.reference import ReferenceRuntime
from nika_core.ui.bridge_models import UIResult

_LOGGER = logging.getLogger(__name__)
_DEFAULT_AGENT_ID = "nika.default"
_DEFAULT_WORKSPACE_ID = "default"
_TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.ARCHIVED}
)
_LOCAL_CANCEL_STATES = frozenset(
    {TaskState.CREATED, TaskState.READY, TaskState.PAUSED, TaskState.BLOCKED}
)


class _DesktopRuntimeLoop:
    """One background asyncio loop for packaged desktop runtime calls.

    pywebview bridge calls are synchronous. Running the canonical async runtime on a dedicated
    loop keeps the bridge responsive without creating a second task/runtime coordinator. All
    live runtime calls, including cancellation and durable resume, stay on the same event loop.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="nika-desktop-runtime",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("desktop runtime event loop did not stop")


class DesktopBackend:
    """Small product-facing facade for the packaged Windows shell.

    The first packaged path uses the deterministic no-LLM runtime so task controls are real
    even without external model credentials. Provider/model selection can replace the runtime
    behind the same coordinator later; the UI never talks to framework objects directly.

    Bridge actions must remain responsive while a runtime is active. The desktop facade therefore
    dispatches canonical ``TaskRuntimeCoordinator`` calls onto one private asyncio loop instead of
    calling ``asyncio.run()`` synchronously on the UI bridge thread.
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
        self._runtime_loop: _DesktopRuntimeLoop | None = None
        self._active_lock = threading.Lock()
        self._active_threads: dict[str, str] = {}
        self._active_futures: dict[str, Future[Any]] = {}
        self._cancel_futures: dict[str, Future[bool]] = {}
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
        self._schedule_start(record.task_id, command)
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message=f"Завдання прийнято до виконання: {command}",
            focus_id="tasks-heading",
        )

    def pause_task(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._only_controllable(action="призупинення")
        if record is None:
            raise ValueError("Немає активного завдання, яке можна призупинити.")
        if record.state == TaskState.RUNNING:
            raise ValueError(
                "Поточний runtime не підтримує безпечне активне призупинення. "
                "Стан RUNNING не змінено; використайте зупинку або runtime із pause/resume."
            )
        if record.state != TaskState.READY:
            raise ValueError(f"Завдання у стані {record.state.value} не можна призупинити.")
        try:
            self._queue.transition(record.task_id, TaskState.PAUSED)
        except ValueError as exc:
            current = self._queue.get(record.task_id)
            if current.state == TaskState.RUNNING:
                raise ValueError(
                    "Runtime уже почав виконання; активне призупинення не підтримується."
                ) from exc
            raise
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Завдання призупинено до початку runtime-виконання.",
            focus_id="tasks-heading",
        )

    def resume_task(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._only_with_state(TaskState.PAUSED, action="продовження")
        if record is None:
            raise ValueError("Немає призупиненого завдання для продовження.")

        session = self._coordinator.sessions.get(record.task_id)
        if session is not None:
            if session.runtime_id != self._runtime.runtime_id:
                raise ValueError(
                    "Збережена runtime-сесія належить іншому runtime; продовження відхилено."
                )
            if RuntimeCapability.DURABLE_RESUME not in self._runtime.capabilities:
                raise ValueError("Поточний runtime не заявляє безпечне durable resume.")
            self._submit_runtime(
                record.task_id,
                session.thread_id,
                self._coordinator.resume_saved(self._runtime, task_id=record.task_id),
            )
            return UIResult(
                request_id="desktop-handler",
                status="accepted",
                message="Збережене runtime-виконання поставлено на безпечне продовження.",
                focus_id="tasks-heading",
            )

        if not self._never_started(record.task_id):
            raise ValueError(
                "Призупинене завдання не має збереженої runtime-сесії; "
                "повторний старт відхилено, щоб не дублювати побічні ефекти."
            )
        command = str(record.payload.get("command", "")).strip()
        if not command:
            raise ValueError("Збережене завдання не містить команди для безпечного запуску.")
        self._queue.transition(record.task_id, TaskState.READY)
        self._schedule_start(record.task_id, command)
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Завдання повернуто до черги виконання.",
            focus_id="tasks-heading",
        )

    def stop_agent(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._only_controllable(action="зупинки")
        if record is None:
            raise ValueError("Немає активного завдання агента для зупинки.")

        cancel_future: Future[bool] | None = None
        with self._active_lock:
            thread_id = self._active_threads.get(record.task_id)
            if thread_id is not None:
                cancel_future = self._schedule_cancel_locked(record.task_id, thread_id)
            else:
                session = self._coordinator.sessions.get(record.task_id)
                if session is not None:
                    if session.runtime_id != self._runtime.runtime_id:
                        raise ValueError(
                            "Активна runtime-сесія належить іншому runtime; "
                            "безпечне скасування відхилено."
                        )
                    cancel_future = self._schedule_cancel_locked(
                        record.task_id, session.thread_id
                    )
                else:
                    current = self._queue.get(record.task_id)
                    if current.state not in _LOCAL_CANCEL_STATES:
                        raise ValueError(
                            "Неможливо безпечно скасувати активне runtime-завдання без "
                            "збереженої або локально активної runtime-сесії."
                        )
                    self._queue.transition(record.task_id, TaskState.CANCELLED)

        if cancel_future is not None:
            cancel_future.add_done_callback(
                lambda done: self._cancel_done(record.task_id, done)
            )
            return UIResult(
                request_id="desktop-handler",
                status="accepted",
                message="Запит на зупинку runtime прийнято.",
                focus_id="tasks-heading",
            )

        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Поточне завдання агента скасовано до активного runtime-виконання.",
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
        """Stop the private bridge event loop after all submitted runtime work has settled."""
        with self._active_lock:
            futures = (
                *self._active_futures.values(),
                *self._cancel_futures.values(),
            )
        for future in futures:
            try:
                future.result(timeout=2)
            except TimeoutError as exc:
                raise RuntimeError(
                    "cannot close desktop runtime loop while tasks are active"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - callback already records bounded failure
                _LOGGER.warning(
                    "Desktop runtime future failed before close; exception_type=%s",
                    type(exc).__name__,
                )
        with self._active_lock:
            self._active_threads.clear()
            self._active_futures.clear()
            self._cancel_futures.clear()
        if self._runtime_loop is not None:
            self._runtime_loop.close()
            self._runtime_loop = None

    def _host(self) -> _DesktopRuntimeLoop:
        if self._runtime_loop is None:
            self._runtime_loop = _DesktopRuntimeLoop()
        return self._runtime_loop

    def _schedule_start(self, task_id: str, command: str) -> None:
        thread_id = f"desktop-{task_id}"
        self._submit_runtime(
            task_id,
            thread_id,
            self._start_if_ready(
                RuntimeRequest(
                    task_id=task_id,
                    thread_id=thread_id,
                    payload={"command": command},
                )
            ),
        )

    async def _start_if_ready(self, request: RuntimeRequest) -> object | None:
        if self._queue.get(request.task_id).state != TaskState.READY:
            return None
        try:
            return await self._coordinator.start(self._runtime, request)
        except ValueError:
            current = self._queue.get(request.task_id)
            if current.state in {TaskState.PAUSED, TaskState.CANCELLED}:
                return None
            raise

    def _schedule_cancel_locked(self, task_id: str, thread_id: str) -> Future[bool]:
        if RuntimeCapability.CANCELLATION not in self._runtime.capabilities:
            raise ValueError("Поточний runtime не заявляє безпечне скасування.")
        existing = self._cancel_futures.get(task_id)
        if existing is not None and not existing.done():
            raise ValueError("Запит на зупинку цього завдання вже виконується.")
        future = self._host().submit(
            self._coordinator.cancel(
                self._runtime,
                task_id=task_id,
                thread_id=thread_id,
            )
        )
        self._cancel_futures[task_id] = future
        return future

    def _submit_runtime(
        self,
        task_id: str,
        thread_id: str,
        coroutine: Coroutine[Any, Any, Any],
    ) -> None:
        with self._active_lock:
            existing = self._active_futures.get(task_id)
            if existing is not None and not existing.done():
                raise ValueError("Завдання вже має активне runtime-виконання.")
            self._active_threads[task_id] = thread_id
            future = self._host().submit(coroutine)
            self._active_futures[task_id] = future
        future.add_done_callback(lambda done: self._runtime_done(task_id, done))

    def _cancel_done(self, task_id: str, future: Future[bool]) -> None:
        with self._active_lock:
            self._cancel_futures.pop(task_id, None)
        if future.cancelled():
            self._record_background_failure(task_id, "desktop.runtime_cancel_interrupted")
            return
        error = future.exception()
        if error is not None:
            self._record_background_failure(task_id, "desktop.runtime_cancel_failed")
            return
        if future.result() is not True:
            self._record_background_failure(task_id, "desktop.runtime_cancel_rejected")

    def _runtime_done(self, task_id: str, future: Future[Any]) -> None:
        with self._active_lock:
            self._active_threads.pop(task_id, None)
            self._active_futures.pop(task_id, None)
        if future.cancelled() or future.exception() is None:
            return
        current = self._queue.get(task_id)
        if current.state == TaskState.RUNNING:
            self._queue.transition(task_id, TaskState.FAILED)
        self._record_background_failure(task_id, "desktop.runtime_host_failed")

    def _record_background_failure(self, task_id: str, event_type: str) -> None:
        self._audit.append(
            event_type=event_type,
            entity_type="task",
            entity_id=task_id,
            payload={"runtime_id": self._runtime.runtime_id},
        )

    def _active_thread(self, task_id: str) -> str | None:
        with self._active_lock:
            return self._active_threads.get(task_id)

    def _never_started(self, task_id: str) -> bool:
        with self._queue.store.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND new_state = ? LIMIT 1",
                (task_id, TaskState.RUNNING.value),
            ).fetchone()
        return row is None

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

    def _only_controllable(self, *, action: str) -> TaskRecord | None:
        records = [
            record
            for record in self._queue.list_recent(limit=50)
            if record.state not in _TERMINAL_STATES
        ]
        return self._require_unambiguous(records, action=action)

    def _only_with_state(self, state: TaskState, *, action: str) -> TaskRecord | None:
        records = [
            record for record in self._queue.list_recent(limit=50) if record.state == state
        ]
        return self._require_unambiguous(records, action=action)

    @staticmethod
    def _require_unambiguous(
        records: list[TaskRecord],
        *,
        action: str,
    ) -> TaskRecord | None:
        if len(records) > 1:
            raise ValueError(
                f"Є кілька завдань, доступних для {action}; потрібен явний вибір завдання."
            )
        return records[0] if records else None

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
