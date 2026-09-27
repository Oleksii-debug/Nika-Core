from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Protocol

from nika_core.background_life import (
    BackgroundAction,
    BackgroundDecision,
    BackgroundWorkKind,
    OwnerPresence,
    decide_background_work,
)
from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceCapacityStatus,
    ResourceSnapshot,
)

_BACKGROUND_SCOPE = "background_life"
_DEFAULT_MAX_PRESENCE_AGE_SECONDS = 5.0
_MAX_CONFIGURED_PRESENCE_AGE_SECONDS = 60.0
_MAX_SIGNED_64 = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class OwnerPresenceObservation:
    """One externally observed owner-presence fact with provenance and freshness."""

    presence: OwnerPresence
    observed_at: datetime
    source_id: str
    revision: int


class OwnerPresenceObserverPort(Protocol):
    def observe(self) -> OwnerPresenceObservation: ...


class ResourceCapacityReaderPort(Protocol):
    def status(self, *, scope: str, owner_id: str) -> ResourceCapacityStatus: ...


@dataclass(frozen=True, slots=True)
class BackgroundEffectOutcome:
    """Result of one effect-time authorization attempt."""

    decision: BackgroundDecision
    executed: bool
    presence_source_id: str
    presence_revision: int
    result: object | None = None


def _require_nonempty_exact_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    if len(value) > 256:
        raise ValueError(f"{name} is too long")
    return value


def _require_revision(value: object) -> int:
    if type(value) is not int:
        raise TypeError("presence revision must be exact built-in int")
    if not 0 <= value <= _MAX_SIGNED_64:
        raise ValueError("presence revision is outside the supported integer range")
    return value


def _require_utc_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be exact datetime")
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _require_presence_age(value: object) -> float:
    if type(value) not in (int, float):
        raise TypeError("max_presence_age_seconds must be exact built-in int or float")
    as_float = float(value)
    if not isfinite(as_float):
        raise ValueError("max_presence_age_seconds must be finite")
    if not 0 < as_float <= _MAX_CONFIGURED_PRESENCE_AGE_SECONDS:
        raise ValueError(
            "max_presence_age_seconds must be in "
            f"(0, {_MAX_CONFIGURED_PRESENCE_AGE_SECONDS:g}]"
        )
    return as_float


def _snapshot_presence(raw: object) -> OwnerPresenceObservation:
    if type(raw) is not OwnerPresenceObservation:
        raise TypeError("presence observer must return exact OwnerPresenceObservation")
    if type(raw.presence) is not OwnerPresence:
        raise TypeError("presence must be exact OwnerPresence")
    observed_at = _require_utc_datetime(raw.observed_at, "presence observed_at")
    source_id = _require_nonempty_exact_str(raw.source_id, "presence source_id")
    revision = _require_revision(raw.revision)
    return OwnerPresenceObservation(
        presence=raw.presence,
        observed_at=observed_at,
        source_id=source_id,
        revision=revision,
    )


def _snapshot_capacity(raw: object) -> ResourceCapacityStatus:
    """Detach canonical resource facts before the background policy consumes them."""

    if type(raw) is not ResourceCapacityStatus:
        raise TypeError("resource reader must return exact ResourceCapacityStatus")
    if type(raw.budget) is not ResourceBudget:
        raise TypeError("resource status budget must be exact ResourceBudget")
    if type(raw.snapshot) is not ResourceSnapshot:
        raise TypeError("resource status snapshot must be exact ResourceSnapshot")

    budget = raw.budget
    snapshot = raw.snapshot
    return ResourceCapacityStatus(
        budget=ResourceBudget(
            scope=budget.scope,
            owner_id=budget.owner_id,
            max_concurrent=budget.max_concurrent,
            max_cpu_percent=budget.max_cpu_percent,
            max_memory_percent=budget.max_memory_percent,
        ),
        snapshot=ResourceSnapshot(
            cpu_percent=snapshot.cpu_percent,
            memory_percent=snapshot.memory_percent,
            available_memory_bytes=snapshot.available_memory_bytes,
            logical_cpu_count=snapshot.logical_cpu_count,
            total_memory_bytes=snapshot.total_memory_bytes,
            process_rss_bytes=snapshot.process_rss_bytes,
            battery_percent=snapshot.battery_percent,
            power_plugged=snapshot.power_plugged,
        ),
        active_count=raw.active_count,
        queued_count=raw.queued_count,
        concurrency_headroom=raw.concurrency_headroom,
        cpu_headroom_percent=raw.cpu_headroom_percent,
        memory_headroom_percent=raw.memory_headroom_percent,
        pressure_reasons=raw.pressure_reasons,
    )


class BackgroundEffectGuard:
    """Revalidate OWNER_AWAY authority immediately before one background effect.

    A prior admission decision is never accepted as effect authority. The run method
    always rereads owner presence and canonical ResourceManager-style capacity
    immediately before executing the supplied effect. The effect is called at most once.
    """

    def __init__(
        self,
        *,
        presence_observer: OwnerPresenceObserverPort,
        resource_reader: ResourceCapacityReaderPort,
        owner_id: str,
        max_presence_age_seconds: int | float = _DEFAULT_MAX_PRESENCE_AGE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._presence_observer = presence_observer
        self._resource_reader = resource_reader
        self._owner_id = _require_nonempty_exact_str(owner_id, "owner_id")
        self._max_presence_age_seconds = _require_presence_age(max_presence_age_seconds)
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def _read_presence(self) -> tuple[OwnerPresenceObservation, datetime]:
        observation = _snapshot_presence(self._presence_observer.observe())
        now = _require_utc_datetime(self._clock(), "trusted clock result")
        return observation, now

    def _decision(
        self,
        work_kind: BackgroundWorkKind,
    ) -> tuple[BackgroundDecision, OwnerPresenceObservation]:
        if type(work_kind) is not BackgroundWorkKind:
            raise TypeError("work_kind must be exact BackgroundWorkKind")

        observation, now = self._read_presence()
        age_seconds = (now - observation.observed_at).total_seconds()

        if age_seconds < 0:
            return (
                BackgroundDecision(
                    action=BackgroundAction.PAUSE,
                    work_kind=work_kind,
                    reason="owner_presence_from_future",
                ),
                observation,
            )
        if age_seconds > self._max_presence_age_seconds:
            return (
                BackgroundDecision(
                    action=BackgroundAction.PAUSE,
                    work_kind=work_kind,
                    reason="owner_presence_stale",
                ),
                observation,
            )

        if observation.presence is not OwnerPresence.AWAY:
            decision = decide_background_work(
                owner_presence=observation.presence,
                work_kind=work_kind,
                capacity=None,  # type: ignore[arg-type]
            )
            return decision, observation

        capacity = _snapshot_capacity(
            self._resource_reader.status(
                scope=_BACKGROUND_SCOPE,
                owner_id=self._owner_id,
            )
        )
        if capacity.budget.owner_id != self._owner_id:
            raise ValueError("resource capacity owner_id does not match background owner")
        decision = decide_background_work(
            owner_presence=observation.presence,
            work_kind=work_kind,
            capacity=capacity,
        )
        return decision, observation

    def preview(self, work_kind: BackgroundWorkKind) -> BackgroundDecision:
        """Return current read-only policy output; never reusable as effect authority."""

        decision, _ = self._decision(work_kind)
        return decision

    def run(
        self,
        work_kind: BackgroundWorkKind,
        effect: Callable[[], object],
    ) -> BackgroundEffectOutcome:
        """Revalidate at effect time and execute the effect at most once on RUN."""

        if not callable(effect):
            raise TypeError("effect must be callable")
        decision, observation = self._decision(work_kind)
        if not decision.allowed:
            return BackgroundEffectOutcome(
                decision=decision,
                executed=False,
                presence_source_id=observation.source_id,
                presence_revision=observation.revision,
            )

        result = effect()
        return BackgroundEffectOutcome(
            decision=decision,
            executed=True,
            presence_source_id=observation.source_id,
            presence_revision=observation.revision,
            result=result,
        )


__all__ = [
    "BackgroundEffectGuard",
    "BackgroundEffectOutcome",
    "OwnerPresenceObservation",
    "OwnerPresenceObserverPort",
    "ResourceCapacityReaderPort",
]
