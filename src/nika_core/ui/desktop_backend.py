from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry
from nika_core.runtime.contracts import RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.reference import ReferenceRuntime
from nika_core.ui.bridge_models import UIResult

_DEFAULT_AGENT_ID = "nika.default"
_DEFAULT_WORKSPACE_ID = "default"
_TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.ARCHIVED}
)


class DesktopBackend:
    """Small product-facing facade for the packaged Windows shell.

    The first packaged path uses the deterministic no-LLM runtime so task controls are real
    even without external model credentials. Provider/model selection can replace the runtime
    behind the same coordinator later; the UI never talks to framework objects directly.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue,
        agents: AgentRegistry,
        workspaces: WorkspaceRegistry,
        audit: AuditLog,
    ) -> None:
        self._queue = queue
        self._agents = agents
        self._workspaces = workspaces
        self._coordinator = TaskRuntimeCoordinator(queue, audit)
        self._runtime = ReferenceRuntime()
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
        result = asyncio.run(
            self._coordinator.start(
                self._runtime,
                RuntimeRequest(
                    task_id=record.task_id,
                    thread_id=f"desktop-{uuid.uuid4()}",
                    payload={"command": command},
                ),
            )
        )
        if result.outcome.value == "completed":
            message = f"Завдання виконано в безпечному режимі без LLM: {command}"
        else:
            message = f"Завдання завершилося зі станом {result.outcome.value}."
        return UIResult(
            request_id="desktop-handler",
            status="completed" if result.outcome.value == "completed" else "failed",
            message=message,
            focus_id="tasks-heading",
        )

    def pause_task(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._latest_controllable()
        if record is None:
            raise ValueError("Немає активного завдання, яке можна призупинити.")
        if record.state not in {TaskState.READY, TaskState.RUNNING}:
            raise ValueError(f"Завдання у стані {record.state.value} не можна призупинити.")
        self._queue.transition(record.task_id, TaskState.PAUSED)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Завдання призупинено.",
            focus_id="tasks-heading",
        )

    def resume_task(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._latest_with_state(TaskState.PAUSED)
        if record is None:
            raise ValueError("Немає призупиненого завдання для продовження.")
        self._queue.transition(record.task_id, TaskState.READY)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Завдання повернуто до черги виконання.",
            focus_id="tasks-heading",
        )

    def stop_agent(self, _payload: Mapping[str, Any]) -> UIResult:
        record = self._latest_controllable()
        if record is None:
            raise ValueError("Немає активного завдання агента для зупинки.")
        if record.state == TaskState.CREATED or record.state in {
            TaskState.READY,
            TaskState.RUNNING,
            TaskState.WAITING_TOOL,
            TaskState.WAITING_APPROVAL,
            TaskState.PAUSED,
            TaskState.RETRYING,
            TaskState.BLOCKED,
        }:
            self._queue.transition(record.task_id, TaskState.CANCELLED)
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
