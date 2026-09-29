from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from nika_core.background_life import BackgroundAction, BackgroundWorkKind
from nika_core.background_runtime import BackgroundDispatchGuard, BackgroundDispatchResult
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceDecision,
    RecurrenceInvocation,
    RecurrenceState,
)

_BINDING_VERSION = 1
_MAX_IDENTITY_LENGTH = 256


BackgroundEffect = Callable[[], Awaitable[object]]
BackgroundEffectResolver = Callable[[str], BackgroundEffect]


def _required_identity(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    if len(value) > _MAX_IDENTITY_LENGTH:
        raise ValueError(f"{name} is too long")
    return value


@dataclass(frozen=True, slots=True)
class BackgroundRecurrenceBinding:
    """Immutable target identity for one durable background admission recurrence."""

    recurrence_id: str
    task_id: str
    work_kind: BackgroundWorkKind
    effect_action_id: str
    owner_id: str = "living-agent"

    def __post_init__(self) -> None:
        _required_identity(self.recurrence_id, "recurrence_id")
        _required_identity(self.task_id, "task_id")
        if type(self.work_kind) is not BackgroundWorkKind:
            raise TypeError("work_kind must be BackgroundWorkKind")
        _required_identity(self.effect_action_id, "effect_action_id")
        _required_identity(self.owner_id, "owner_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "version": _BINDING_VERSION,
            "recurrence_id": self.recurrence_id,
            "task_id": self.task_id,
            "work_kind": self.work_kind.value,
            "effect_action_id": self.effect_action_id,
            "owner_id": self.owner_id,
        }

    @classmethod
    def from_payload(cls, raw: object) -> BackgroundRecurrenceBinding:
        if type(raw) is not dict:
            raise TypeError("background recurrence payload must be exact built-in dict")
        if any(type(key) is not str for key in raw):
            raise TypeError("background recurrence payload keys must be exact built-in str")
        expected_keys = {
            "version",
            "recurrence_id",
            "task_id",
            "work_kind",
            "effect_action_id",
            "owner_id",
        }
        if set(raw) != expected_keys:
            raise ValueError("background recurrence payload has unexpected fields")
        version = raw["version"]
        if type(version) is not int or version != _BINDING_VERSION:
            raise ValueError("unsupported background recurrence payload version")
        raw_work_kind = raw["work_kind"]
        if type(raw_work_kind) is not str:
            raise TypeError("background recurrence work_kind must be exact built-in str")
        try:
            work_kind = BackgroundWorkKind(raw_work_kind)
        except ValueError as exc:
            raise ValueError("background recurrence work_kind is invalid") from exc
        return cls(
            recurrence_id=_required_identity(raw["recurrence_id"], "recurrence_id"),
            task_id=_required_identity(raw["task_id"], "task_id"),
            work_kind=work_kind,
            effect_action_id=_required_identity(raw["effect_action_id"], "effect_action_id"),
            owner_id=_required_identity(raw["owner_id"], "owner_id"),
        )


class BackgroundRecurrenceBridge:
    """Connect canonical durable recurrence to the guarded one-shot background effect.

    A recurrence is an admission/retry loop for one existing task. It stops after the
    first successful guarded effect (or when #855 reports that effect already completed).
    It never turns one task into a repeating side effect.

    Occurrence execution is intentionally not a public bridge API. DurableRecurrenceService
    owns persisted binding/cursor/deadline/due-time authority and is the only supported caller
    of the private handler returned by resolve().
    """

    ACTION_ID = "living.background.dispatch"

    def __init__(
        self,
        *,
        guard: BackgroundDispatchGuard,
        effect_resolver: BackgroundEffectResolver,
    ) -> None:
        if type(guard) is not BackgroundDispatchGuard:
            raise TypeError("guard must be exact BackgroundDispatchGuard")
        if not callable(effect_resolver):
            raise TypeError("effect_resolver must be callable")
        queue = getattr(guard, "_queue", None)
        if type(queue) is not TaskQueue:
            raise TypeError("guard must retain the canonical TaskQueue authority")
        self._guard = guard
        self._queue = queue
        self._effect_resolver = effect_resolver

    def create(
        self,
        recurrence: DurableRecurrenceService,
        *,
        recurrence_id: str,
        task_id: str,
        work_kind: BackgroundWorkKind,
        effect_action_id: str,
        interval_seconds: int,
        start_at: datetime,
        owner_id: str = "living-agent",
        deadline_at: datetime | None = None,
    ) -> RecurrenceState:
        if type(recurrence) is not DurableRecurrenceService:
            raise TypeError("recurrence must be exact DurableRecurrenceService")
        binding = BackgroundRecurrenceBinding(
            recurrence_id=recurrence_id,
            task_id=task_id,
            work_kind=work_kind,
            effect_action_id=effect_action_id,
            owner_id=owner_id,
        )
        task = self._queue.get(binding.task_id)
        if self._is_irreversibly_terminal(task.state):
            raise ValueError("background recurrence cannot bind an irreversibly terminal task")
        return recurrence.create(
            recurrence_id=binding.recurrence_id,
            task_id=binding.task_id,
            action_id=self.ACTION_ID,
            interval_seconds=interval_seconds,
            start_at=start_at,
            payload=binding.to_payload(),
            deadline_at=deadline_at,
        )

    def resolve(
        self,
        action_id: str,
    ) -> Callable[[RecurrenceInvocation], RecurrenceDecision]:
        action_key = _required_identity(action_id, "action_id")
        if action_key != self.ACTION_ID:
            raise KeyError(f"unknown background recurrence action: {action_key}")
        return self._occurrence_handler

    def _occurrence_handler(self, invocation: RecurrenceInvocation) -> RecurrenceDecision:
        """Trusted callback for DurableRecurrenceService only."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "canonical background recurrence handler requires a non-async scheduler thread"
            )
        snapshot = self._snapshot_invocation(invocation)
        return asyncio.run(self._dispatch_trusted_invocation(snapshot))

    async def _dispatch_trusted_invocation(
        self,
        invocation: RecurrenceInvocation,
    ) -> RecurrenceDecision:
        binding = BackgroundRecurrenceBinding.from_payload(invocation.payload)
        if binding.recurrence_id != invocation.recurrence_id:
            raise ValueError("background recurrence identity mismatch")
        if self._is_irreversibly_terminal(self._queue.get(binding.task_id).state):
            return RecurrenceDecision.STOP

        async def guarded_effect() -> object:
            effect = self._effect_resolver(binding.effect_action_id)
            if not callable(effect):
                raise TypeError("effect_resolver must return a callable")
            produced = effect()
            if not inspect.isawaitable(produced):
                raise TypeError("background effect must return an awaitable")
            return await produced

        result = await self._guard.dispatch(
            task_id=binding.task_id,
            work_kind=binding.work_kind,
            effect=guarded_effect,
            owner_id=binding.owner_id,
        )
        return self._decision_from_dispatch(result)

    @staticmethod
    def _is_irreversibly_terminal(state: object) -> bool:
        return state in {
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.ARCHIVED,
        }

    @staticmethod
    def _snapshot_invocation(raw: object) -> RecurrenceInvocation:
        if type(raw) is not RecurrenceInvocation:
            raise TypeError("invocation must be exact RecurrenceInvocation")
        recurrence_id = _required_identity(raw.recurrence_id, "invocation recurrence_id")
        occurrence_id = _required_identity(raw.occurrence_id, "invocation occurrence_id")
        if type(raw.scheduled_for) is not datetime:
            raise TypeError("invocation scheduled_for must be exact built-in datetime")
        if raw.scheduled_for.tzinfo is None or raw.scheduled_for.utcoffset() is None:
            raise ValueError("invocation scheduled_for must be timezone-aware")
        if raw.scheduled_for.utcoffset().total_seconds() != 0:
            raise ValueError("invocation scheduled_for must use UTC")
        if type(raw.payload) is not dict:
            raise TypeError("invocation payload must be exact built-in dict")
        return RecurrenceInvocation(
            recurrence_id=recurrence_id,
            occurrence_id=occurrence_id,
            scheduled_for=raw.scheduled_for,
            payload=dict(raw.payload),
        )

    @staticmethod
    def _decision_from_dispatch(raw: object) -> RecurrenceDecision:
        if type(raw) is not BackgroundDispatchResult:
            raise TypeError("guard returned a non-canonical BackgroundDispatchResult")
        if type(raw.action) is not BackgroundAction:
            raise TypeError("background dispatch action is non-canonical")
        if type(raw.reason) is not str or not raw.reason or raw.reason != raw.reason.strip():
            raise ValueError("background dispatch reason is non-canonical")
        if type(raw.effect_started) is not bool:
            raise TypeError("background dispatch effect_started must be exact built-in bool")

        if raw.action is BackgroundAction.RUN:
            if not raw.effect_started:
                raise RuntimeError("RUN dispatch result must have started its effect")
            return RecurrenceDecision.STOP

        if raw.effect_started:
            raise RuntimeError("non-RUN dispatch result cannot have started its effect")
        if raw.action is BackgroundAction.DEFER and raw.reason == "dispatch_already_completed":
            return RecurrenceDecision.STOP
        if raw.action in {BackgroundAction.PAUSE, BackgroundAction.DEFER}:
            return RecurrenceDecision.CONTINUE
        raise ValueError("unsupported background dispatch action")
