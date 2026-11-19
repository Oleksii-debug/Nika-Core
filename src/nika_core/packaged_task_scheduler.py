"""Ordinary desktop-task scheduling over Nika's incumbent durable scheduler.

One intent admits one already-prepared task. The interval retries admission only;
it never requests another semantic execution of an already-started task.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceDecision,
    RecurrenceInvocation,
    RecurrenceState,
    RecurrenceStatus,
    _job_id,
)
from nika_core.scheduler.store import ScheduledJobStore

ScheduledTaskStarter = Callable[[str], bool]
_TERMINAL_TASKS = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.ARCHIVED}
)
_PREFIX = "desktop-task:"


class PackagedTaskScheduler:
    """Thin single-host composition, adapted from canonical #883/#860.

    ``start_task`` is a trusted desktop-backend port, never a client callback.
    It runs on the scheduler worker and must return after bounded admission proof,
    without waiting for a potentially long-running runtime to finish.
    True acknowledges durable start/already-started truth; False means a proven
    pre-effect resource wait. Exceptions leave the occurrence unadvanced.
    """

    START_ACTION_ID = "desktop.scheduled_task.start"

    def __init__(
        self,
        *,
        store: SQLiteStore,
        audit: AuditLog,
        start_task: ScheduledTaskStarter,
        workspace_id: str = "default",
        agent_id: str = "nika.default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(store) is not SQLiteStore or type(audit) is not AuditLog:
            raise TypeError("Планувальнику потрібні канонічні сховище та журнал.")
        if audit._store is not store:
            raise ValueError("Планувальник і журнал повинні використовувати спільне сховище.")
        if not callable(start_task):
            raise TypeError("Потрібен довірений обробник запуску завдання.")
        self._workspace_id = _identity(workspace_id)
        self._agent_id = _identity(agent_id)
        self._store = store
        self._queue = TaskQueue(store)
        self._start_task = start_task
        self._jobs = ScheduledJobStore(store)
        self._scheduler = APSchedulerAdapter(
            self._jobs, self._resolve_scheduler_action, audit=audit
        )
        self._recurrence = DurableRecurrenceService(
            jobs=self._jobs,
            scheduler=self._scheduler,
            handler_resolver=self._resolve_task_action,
            clock=clock,
        )

    def start(self) -> None:
        """Reinstall durable intents through APScheduler; do not replay runtimes."""
        self._scheduler.start()

    def shutdown(self, *, wait: bool = True) -> None:
        if type(wait) is not bool:
            raise TypeError("Параметр очікування повинен бути логічним значенням.")
        self._scheduler.shutdown(wait=wait)

    def create(
        self,
        *,
        task_id: str,
        start_at: datetime,
        retry_interval_seconds: int = 60,
        deadline_at: datetime | None = None,
    ) -> RecurrenceState:
        """Schedule one READY ordinary task without starting or creating another task."""
        task_key = _task_id(task_id)
        task = self._ordinary_task(task_key)
        existing = self._recurrence.get(_recurrence_id(task_key))
        if existing is None and (
            task.state is not TaskState.READY or self._has_started(task_key)
        ):
            raise ValueError("Планувати можна лише підготовлене завдання, яке ще не запускалось.")
        return self._recurrence.create(
            recurrence_id=_recurrence_id(task_key),
            task_id=task_key,
            action_id=self.START_ACTION_ID,
            interval_seconds=retry_interval_seconds,
            start_at=start_at,
            payload={"task_id": task_key},
            deadline_at=deadline_at,
        )

    def get(self, task_id: str) -> RecurrenceState | None:
        return self._recurrence.get(self._controlled_recurrence_id(task_id))

    def pause(self, task_id: str) -> RecurrenceState:
        """Pause future admission attempts; never claim to pause a running runtime."""
        return self._recurrence.pause(self._controlled_recurrence_id(task_id))

    def resume(self, task_id: str) -> RecurrenceState:
        state = self._recurrence.resume(self._controlled_recurrence_id(task_id))
        if state.status is RecurrenceStatus.ACTIVE:
            # A failed DATE callback is consumed by APScheduler, while the
            # unadvanced durable occurrence correctly remains ACTIVE. Explicit
            # resume must reinstall it, rather than treating ACTIVE as a no-op.
            # Only activate current persisted authority; never upsert a snapshot.
            job = self._jobs.get(_job_id(state.recurrence_id))
            if job is not None and job.enabled:
                self._scheduler.activate_persisted(job)
        current = self._recurrence.get(state.recurrence_id)
        if current is None:
            raise ValueError("Запланований запуск більше не існує.")
        return current

    def cancel(self, task_id: str) -> RecurrenceState:
        """Cancel the timer only. Live runtime Stop remains coordinator authority."""
        return self._recurrence.cancel(self._controlled_recurrence_id(task_id))

    def snapshot(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Compatibility first page; use snapshot_page to navigate further rows."""
        return self.snapshot_page(limit=limit)["items"]

    def snapshot_page(
        self, *, limit: int = 50, after_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Keyset-page actual schedule IDs, never unrelated recent task history.

        The cursor selects a view only and never authorizes dispatch. Filtering
        foreign recurrence namespaces can yield an empty page with a next cursor.
        SQL bounds candidates before strict durable decoding/materialization.
        """
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Кількість записів розкладу повинна бути від 1 до 50.")
        cursor = _page_cursor(after_job_id)
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT job_id FROM scheduled_jobs WHERE action_id = ? "
                "AND job_id LIKE 'recurrence-v1:%' AND job_id > ? "
                "ORDER BY job_id LIMIT ?",
                (DurableRecurrenceService.ACTION_ID, cursor, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        result: list[dict[str, Any]] = []
        for row in selected:
            job = self._jobs.get(_page_cursor(row["job_id"]))
            if job is None:
                continue
            recurrence_id = job.payload.get("recurrence_id")
            if type(recurrence_id) is not str or not recurrence_id.startswith(_PREFIX):
                continue
            key = _task_id(recurrence_id[len(_PREFIX):])
            if job.job_id != _job_id(recurrence_id) or job.payload.get("task_id") != key:
                raise ValueError("Пошкоджена ідентичність збереженого розкладу.")
            try:
                task = self._queue.get(key)
            except KeyError:
                task = None
            if task is not None and (
                task.workspace_id != self._workspace_id or task.agent_id != self._agent_id
            ):
                continue
            state = self.get(key)
            if state is not None:
                result.append(
                    {
                        "task_id": state.task_id,
                        "status": state.status.value,
                        "next_due_at": (
                            state.next_due_at.isoformat() if state.next_due_at else None
                        ),
                        "deadline_at": (
                            state.deadline_at.isoformat() if state.deadline_at else None
                        ),
                        "retry_interval_seconds": state.interval_seconds,
                    }
                )
        has_more = len(rows) > limit
        return {
            "items": result,
            "next_after_job_id": selected[-1]["job_id"] if has_more else None,
            "has_more": has_more,
        }

    def _resolve_scheduler_action(self, action_id: str):
        if type(action_id) is not str or action_id != DurableRecurrenceService.ACTION_ID:
            raise ValueError("Невідомий обробник запланованої дії.")
        return self._recurrence.action_handler

    def _resolve_task_action(self, action_id: str):
        if type(action_id) is not str or action_id != self.START_ACTION_ID:
            raise ValueError("Невідомий обробник запланованого завдання.")
        return self._dispatch_task

    def _dispatch_task(self, invocation: RecurrenceInvocation) -> RecurrenceDecision:
        if type(invocation) is not RecurrenceInvocation or type(invocation.payload) is not dict:
            raise TypeError("Пошкоджене підтвердження запланованого запуску.")
        if set(invocation.payload) != {"task_id"}:
            raise ValueError("Пошкоджене прив'язування запланованого завдання.")
        task_key = _task_id(invocation.payload["task_id"])
        if invocation.recurrence_id != _recurrence_id(task_key):
            raise ValueError("Ідентичність запланованого завдання не збігається.")
        task = self._ordinary_task(task_key)
        if task.state in _TERMINAL_TASKS or self._has_started(task_key):
            return RecurrenceDecision.STOP
        admitted = self._start_task(task_key)
        if type(admitted) is not bool:
            raise TypeError("Обробник запуску повернув непідтверджений результат.")
        current = self._ordinary_task(task_key)
        started = self._has_started(task_key) or current.state in _TERMINAL_TASKS
        if admitted:
            if not started:
                raise RuntimeError("Запуск не підтверджено збереженим станом завдання.")
            return RecurrenceDecision.STOP
        if started:
            raise RuntimeError("Відмова запуску суперечить збереженому стану завдання.")
        return RecurrenceDecision.CONTINUE

    def _ordinary_task(self, task_id: str) -> TaskRecord:
        task = self._queue.get(task_id)
        if task.workspace_id != self._workspace_id or task.agent_id != self._agent_id:
            raise ValueError("Завдання належить іншому робочому простору або агенту.")
        return task

    def _controlled_recurrence_id(self, task_id: str) -> str:
        task_key = _task_id(task_id)
        try:
            self._ordinary_task(task_key)
        except KeyError:
            # Canonical recurrence recovery may expose a missing task as cancelled.
            # No missing task can acquire a fresh runtime through this host.
            pass
        return _recurrence_id(task_key)

    def _has_started(self, task_id: str) -> bool:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND new_state = ? LIMIT 1",
                (task_id, TaskState.RUNNING.value),
            ).fetchone()
        return row is not None


def _identity(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("Ідентичність повинна бути непорожнім точним текстом.")
    return value


def _task_id(value: object) -> str:
    text = _identity(value)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError("Некоректний ідентифікатор завдання.") from exc
    if str(parsed) != text:
        raise ValueError("Потрібен канонічний ідентифікатор завдання.")
    return text


def _recurrence_id(task_id: str) -> str:
    return _PREFIX + task_id


def _page_cursor(value: object) -> str:
    if value is None:
        return ""
    prefix = "recurrence-v1:"
    if (
        type(value) is not str or not value.startswith(prefix)
        or len(value) != len(prefix) + 64
        or any(char not in "0123456789abcdef" for char in value[len(prefix):])
    ):
        raise ValueError("Некоректний покажчик сторінки розкладу.")
    return value
