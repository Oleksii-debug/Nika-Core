from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite

from nika_core.background_life import OwnerPresence
from nika_core.background_runtime import OwnerPresenceObservation
from nika_core.kernel.audit import AuditLog
from nika_core.runtime.contracts import AgentRuntimePort
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.windows_owner_presence import WindowsOwnerPresenceObserver

_MAX_CONFIGURED_PRESENCE_AGE_SECONDS = 60.0


class RunningBackgroundAction(StrEnum):
    CONTINUE = "continue"
    PAUSED = "paused"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True, slots=True)
class RunningBackgroundReconcileResult:
    action: RunningBackgroundAction
    reason: str
    pause_applied: bool


class WindowsBackgroundOwnerReturnController:
    """Pause already-running background work when physical owner presence is not safely AWAY.

    The controller owns no task/runtime state transition. It samples the canonical Win32
    presence adapter and delegates every active pause effect to TaskRuntimeCoordinator.pause().
    """

    def __init__(
        self,
        *,
        coordinator: TaskRuntimeCoordinator,
        audit: AuditLog,
        presence: WindowsOwnerPresenceObserver,
        max_presence_age_seconds: int | float = 5.0,
        max_future_skew_seconds: int | float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(presence) is not WindowsOwnerPresenceObserver:
            raise TypeError("presence must be exact WindowsOwnerPresenceObserver")
        self._max_age = self._bounded_seconds(
            max_presence_age_seconds,
            "max_presence_age_seconds",
            allow_zero=False,
        )
        self._max_future_skew = self._bounded_seconds(
            max_future_skew_seconds,
            "max_future_skew_seconds",
            allow_zero=True,
        )
        if self._max_future_skew > self._max_age:
            raise ValueError("max_future_skew_seconds cannot exceed max_presence_age_seconds")
        self._coordinator = coordinator
        self._audit = audit
        self._presence = presence
        self._source_id = presence.source_id
        self._clock = clock or (lambda: datetime.now(UTC))

    async def reconcile(
        self,
        *,
        runtime: AgentRuntimePort,
        task_id: str,
        thread_id: str,
    ) -> RunningBackgroundReconcileResult:
        self._require_identity(task_id, "task_id")
        self._require_identity(thread_id, "thread_id")
        self._require_background_runtime_provenance(
            runtime=runtime,
            task_id=task_id,
            thread_id=thread_id,
        )

        reason: str
        try:
            observation = self._presence.observe()
            self._validate_observation(observation)
        except Exception as exc:  # noqa: BLE001 - physical/evidence boundary fails closed
            reason = "owner_presence_untrusted"
            self._audit.append(
                event_type="background.running_presence_rejected",
                entity_type="task",
                entity_id=task_id,
                payload={"error_type": type(exc).__name__},
            )
        else:
            if observation.presence is OwnerPresence.AWAY:
                self._audit.append(
                    event_type="background.running_owner_away",
                    entity_type="task",
                    entity_id=task_id,
                    payload={"source_id": self._source_id},
                )
                return RunningBackgroundReconcileResult(
                    action=RunningBackgroundAction.CONTINUE,
                    reason="owner_away",
                    pause_applied=False,
                )
            reason = (
                "owner_active"
                if observation.presence is OwnerPresence.ACTIVE
                else "owner_presence_unknown"
            )

        try:
            applied = await self._coordinator.pause(
                runtime,
                task_id=task_id,
                thread_id=thread_id,
            )
        except Exception as exc:
            self._audit.append(
                event_type="background.running_pause_failed",
                entity_type="task",
                entity_id=task_id,
                payload={"reason": reason, "error_type": type(exc).__name__},
            )
            raise

        if not applied:
            self._audit.append(
                event_type="background.running_pause_not_active",
                entity_type="task",
                entity_id=task_id,
                payload={"reason": reason},
            )
            return RunningBackgroundReconcileResult(
                action=RunningBackgroundAction.NOT_ACTIVE,
                reason=reason,
                pause_applied=False,
            )

        self._audit.append(
            event_type="background.running_paused_for_owner",
            entity_type="task",
            entity_id=task_id,
            payload={"reason": reason, "source_id": self._source_id},
        )
        return RunningBackgroundReconcileResult(
            action=RunningBackgroundAction.PAUSED,
            reason=reason,
            pause_applied=True,
        )

    def _require_background_runtime_provenance(
        self,
        *,
        runtime: AgentRuntimePort,
        task_id: str,
        thread_id: str,
    ) -> None:
        runtime_id = getattr(runtime, "runtime_id", None)
        self._require_identity(runtime_id, "runtime.runtime_id")
        events = self._audit.list_for(entity_type="task", entity_id=task_id)

        permitted_id: int | None = None
        for event in events:
            if event.event_type == "background.dispatch_permitted":
                permitted_id = event.event_id

        if permitted_id is None:
            raise ValueError("task lacks durable background dispatch provenance")

        for event in events:
            if event.event_id <= permitted_id or event.event_type != "runtime.started":
                continue
            event_runtime_id = event.payload.get("runtime_id")
            event_thread_id = event.payload.get("thread_id")
            if (
                type(event_runtime_id) is str
                and type(event_thread_id) is str
                and event_runtime_id == runtime_id
                and event_thread_id == thread_id
            ):
                return
        raise ValueError(
            "task lacks matching runtime start after background dispatch permission"
        )

    def _validate_observation(self, observation: object) -> None:
        if type(observation) is not OwnerPresenceObservation:
            raise TypeError("presence observer returned a non-canonical observation")
        if observation.source_id != self._source_id:
            raise ValueError("presence observation source changed")

        now = self._clock()
        if type(now) is not datetime:
            raise TypeError("controller clock must return exact datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("controller clock must return timezone-aware datetime")
        if now.utcoffset().total_seconds() != 0:
            raise ValueError("controller clock must use UTC")

        age = (now - observation.observed_at).total_seconds()
        if age > self._max_age:
            raise ValueError("presence observation is stale")
        if age < -self._max_future_skew:
            raise ValueError("presence observation is too far in the future")

    @staticmethod
    def _require_identity(value: object, name: str) -> str:
        if type(value) is not str:
            raise TypeError(f"{name} must be exact built-in str")
        if not value or value != value.strip():
            raise ValueError(f"{name} must be non-empty without surrounding whitespace")
        return value

    @staticmethod
    def _bounded_seconds(value: object, name: str, *, allow_zero: bool) -> float:
        if type(value) not in (int, float):
            raise TypeError(f"{name} must be exact built-in int or float")
        if type(value) is float and not isfinite(value):
            raise ValueError(f"{name} must be finite")
        lower_ok = value >= 0 if allow_zero else value > 0
        if not lower_ok or value > _MAX_CONFIGURED_PRESENCE_AGE_SECONDS:
            lower = "[0" if allow_zero else "(0"
            raise ValueError(
                f"{name} must be in {lower}, {_MAX_CONFIGURED_PRESENCE_AGE_SECONDS}] seconds"
            )
        return float(value)
