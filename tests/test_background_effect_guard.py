from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.background_effect_guard import (
    BackgroundEffectGuard,
    OwnerPresenceObservation,
)
from nika_core.background_life import BackgroundAction, BackgroundWorkKind, OwnerPresence
from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceCapacityStatus,
    ResourceSnapshot,
)

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)


def capacity(
    *,
    owner_id: str = "living-agent",
    active_count: int = 0,
    cpu_percent: float = 10.0,
    power_plugged: bool | None = True,
) -> ResourceCapacityStatus:
    max_concurrent = 1
    max_cpu = 80.0
    reasons: list[str] = []
    if active_count >= max_concurrent:
        reasons.append("concurrency_limit")
    if cpu_percent > max_cpu:
        reasons.append("cpu_limit")
    return ResourceCapacityStatus(
        budget=ResourceBudget(
            scope="background_life",
            owner_id=owner_id,
            max_concurrent=max_concurrent,
            max_cpu_percent=max_cpu,
            max_memory_percent=80.0,
        ),
        snapshot=ResourceSnapshot(
            cpu_percent=cpu_percent,
            memory_percent=20.0,
            available_memory_bytes=8 * 1024 * 1024 * 1024,
            power_plugged=power_plugged,
        ),
        active_count=active_count,
        queued_count=0,
        concurrency_headroom=max(0, max_concurrent - active_count),
        cpu_headroom_percent=max_cpu - cpu_percent,
        memory_headroom_percent=60.0,
        pressure_reasons=tuple(reasons),
    )


@dataclass
class PresenceObserver:
    observation: OwnerPresenceObservation
    calls: int = 0

    def observe(self) -> OwnerPresenceObservation:
        self.calls += 1
        return self.observation


@dataclass
class ResourceReader:
    current: ResourceCapacityStatus
    calls: int = 0

    def status(self, *, scope: str, owner_id: str) -> ResourceCapacityStatus:
        self.calls += 1
        assert scope == "background_life"
        assert owner_id == "living-agent"
        return self.current


def observation(
    presence: OwnerPresence = OwnerPresence.AWAY,
    *,
    observed_at: datetime = NOW,
    source_id: str = "windows-session-presence",
    revision: int = 7,
) -> OwnerPresenceObservation:
    return OwnerPresenceObservation(
        presence=presence,
        observed_at=observed_at,
        source_id=source_id,
        revision=revision,
    )


def guard(
    presence: OwnerPresenceObservation,
    resources: ResourceReader,
    *,
    max_age: float = 5.0,
) -> tuple[BackgroundEffectGuard, PresenceObserver]:
    observer = PresenceObserver(presence)
    return (
        BackgroundEffectGuard(
            presence_observer=observer,
            resource_reader=resources,
            owner_id="living-agent",
            presence_source_id="windows-session-presence",
            max_presence_age_seconds=max_age,
            clock=lambda: NOW,
        ),
        observer,
    )


def test_run_executes_once_only_after_fresh_away_and_capacity_revalidation() -> None:
    resources = ResourceReader(capacity())
    service, observer = guard(observation(), resources)
    effects: list[str] = []

    outcome = service.run(
        BackgroundWorkKind.READING_RESEARCH,
        lambda: effects.append("ran") or "result",
    )

    assert outcome.executed is True
    assert outcome.result == "result"
    assert outcome.decision.action is BackgroundAction.RUN
    assert outcome.presence_source_id == "windows-session-presence"
    assert outcome.presence_revision == 7
    assert observer.calls == 1
    assert resources.calls == 1
    assert effects == ["ran"]


@pytest.mark.parametrize(
    ("presence", "reason"),
    [
        (OwnerPresence.ACTIVE, "owner_active"),
        (OwnerPresence.UNKNOWN, "owner_presence_unknown"),
    ],
)
def test_active_or_unknown_presence_pauses_before_capacity_or_effect(
    presence: OwnerPresence,
    reason: str,
) -> None:
    resources = ResourceReader(capacity())
    service, _ = guard(observation(presence), resources)
    effects: list[str] = []

    outcome = service.run(
        BackgroundWorkKind.SELF_TEST,
        lambda: effects.append("forbidden"),
    )

    assert outcome.executed is False
    assert outcome.decision.action is BackgroundAction.PAUSE
    assert outcome.decision.reason == reason
    assert resources.calls == 0
    assert effects == []


@pytest.mark.parametrize(
    ("observed_at", "reason"),
    [
        (NOW - timedelta(seconds=5.001), "owner_presence_stale"),
        (NOW + timedelta(microseconds=1), "owner_presence_from_future"),
    ],
)
def test_stale_or_future_presence_pauses_without_capacity_or_effect(
    observed_at: datetime,
    reason: str,
) -> None:
    resources = ResourceReader(capacity())
    service, _ = guard(observation(observed_at=observed_at), resources)
    effects: list[str] = []

    outcome = service.run(
        BackgroundWorkKind.MEMORY_CONSOLIDATION,
        lambda: effects.append("forbidden"),
    )

    assert outcome.executed is False
    assert outcome.decision.action is BackgroundAction.PAUSE
    assert outcome.decision.reason == reason
    assert resources.calls == 0
    assert effects == []


def test_prior_preview_is_not_reused_after_resource_pressure_changes() -> None:
    resources = ResourceReader(capacity())
    service, observer = guard(observation(), resources)
    effects: list[str] = []

    preview = service.preview(BackgroundWorkKind.READING_RESEARCH)
    assert preview.action is BackgroundAction.RUN

    resources.current = capacity(cpu_percent=95.0)
    outcome = service.run(
        BackgroundWorkKind.READING_RESEARCH,
        lambda: effects.append("forbidden"),
    )

    assert outcome.executed is False
    assert outcome.decision.action is BackgroundAction.DEFER
    assert outcome.decision.reason == "resource_pressure"
    assert observer.calls == 2
    assert resources.calls == 2
    assert effects == []


def test_prior_preview_is_not_reused_after_owner_becomes_active() -> None:
    resources = ResourceReader(capacity())
    service, observer = guard(observation(), resources)
    effects: list[str] = []

    assert service.preview(BackgroundWorkKind.SELF_TEST).allowed is True
    observer.observation = observation(OwnerPresence.ACTIVE, revision=8)

    outcome = service.run(
        BackgroundWorkKind.SELF_TEST,
        lambda: effects.append("forbidden"),
    )

    assert outcome.executed is False
    assert outcome.decision.reason == "owner_active"
    assert resources.calls == 1
    assert effects == []


def test_high_impact_work_reuses_parent_power_policy_at_effect_time() -> None:
    resources = ResourceReader(capacity(power_plugged=False))
    service, _ = guard(observation(), resources)

    outcome = service.run(
        BackgroundWorkKind.BOUNDED_ML_PILOT,
        lambda: pytest.fail("effect must not run on battery"),
    )

    assert outcome.executed is False
    assert outcome.decision.action is BackgroundAction.DEFER
    assert outcome.decision.reason == "battery_power"


def test_effect_exception_propagates_without_retry() -> None:
    resources = ResourceReader(capacity())
    service, _ = guard(observation(), resources)
    calls = 0

    def failing_effect() -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("effect failed")

    with pytest.raises(RuntimeError, match="effect failed"):
        service.run(BackgroundWorkKind.EVIDENCE_VERIFICATION, failing_effect)

    assert calls == 1


def test_presence_snapshot_is_detached_from_provider_alias_mutation() -> None:
    raw = observation()

    class MutatingReader(ResourceReader):
        def status(self, *, scope: str, owner_id: str) -> ResourceCapacityStatus:
            object.__setattr__(raw, "presence", OwnerPresence.ACTIVE)
            object.__setattr__(raw, "revision", 99)
            return super().status(scope=scope, owner_id=owner_id)

    resources = MutatingReader(capacity())
    service, _ = guard(raw, resources)

    outcome = service.run(BackgroundWorkKind.READING_RESEARCH, lambda: "ok")

    assert outcome.executed is True
    assert outcome.presence_revision == 7
    assert outcome.result == "ok"


@pytest.mark.parametrize(
    "bad",
    [
        observation(source_id=" bad"),
        observation(revision=-1),
        observation(observed_at=datetime(2026, 9, 27, 18, 0)),
    ],
)
def test_malformed_presence_evidence_fails_before_resource_or_effect(
    bad: OwnerPresenceObservation,
) -> None:
    resources = ResourceReader(capacity())
    service, _ = guard(bad, resources)
    effects: list[str] = []

    with pytest.raises((TypeError, ValueError)):
        service.run(
            BackgroundWorkKind.SELF_TEST,
            lambda: effects.append("forbidden"),
        )

    assert resources.calls == 0
    assert effects == []


def test_behavioral_presence_subclass_is_rejected_before_resource_or_effect() -> None:
    class ForgedObservation(OwnerPresenceObservation):
        pass

    raw = observation()
    forged = ForgedObservation(
        raw.presence,
        raw.observed_at,
        raw.source_id,
        raw.revision,
    )
    resources = ResourceReader(capacity())
    observer = PresenceObserver(forged)
    service = BackgroundEffectGuard(
        presence_observer=observer,
        resource_reader=resources,
        owner_id="living-agent",
        presence_source_id="windows-session-presence",
        clock=lambda: NOW,
    )

    with pytest.raises(TypeError, match="exact OwnerPresenceObservation"):
        service.run(BackgroundWorkKind.SELF_TEST, lambda: "forbidden")

    assert resources.calls == 0


def test_malformed_capacity_fails_before_effect() -> None:
    status = capacity(active_count=1)
    object.__setattr__(status, "concurrency_headroom", 1)
    object.__setattr__(status, "pressure_reasons", ())
    resources = ResourceReader(status)
    service, _ = guard(observation(), resources)

    with pytest.raises(ValueError, match="concurrency_headroom"):
        service.run(BackgroundWorkKind.SELF_TEST, lambda: "forbidden")


@pytest.mark.parametrize(
    "value",
    [0, -1, float("nan"), float("inf"), 60.001, True],
)
def test_presence_freshness_configuration_is_bounded(value: object) -> None:
    resources = ResourceReader(capacity())

    with pytest.raises((TypeError, ValueError)):
        BackgroundEffectGuard(
            presence_observer=PresenceObserver(observation()),
            resource_reader=resources,
            owner_id="living-agent",
            presence_source_id="windows-session-presence",
            max_presence_age_seconds=value,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )


def test_capacity_for_different_owner_cannot_authorize_effect() -> None:
    resources = ResourceReader(capacity(owner_id="other-owner"))
    service, _ = guard(observation(), resources)
    effects: list[str] = []

    with pytest.raises(ValueError, match="owner_id"):
        service.run(
            BackgroundWorkKind.READING_RESEARCH,
            lambda: effects.append("forbidden"),
        )

    assert effects == []


def test_untrusted_presence_source_cannot_authorize_effect() -> None:
    resources = ResourceReader(capacity())
    service, _ = guard(observation(source_id="untrusted-source"), resources)
    effects: list[str] = []

    with pytest.raises(ValueError, match="trusted presence source"):
        service.run(
            BackgroundWorkKind.READING_RESEARCH,
            lambda: effects.append("forbidden"),
        )

    assert resources.calls == 0
    assert effects == []


def test_presence_revision_cannot_move_backwards_after_prior_observation() -> None:
    resources = ResourceReader(capacity())
    service, observer = guard(observation(revision=7), resources)

    assert service.preview(BackgroundWorkKind.SELF_TEST).allowed is True
    observer.observation = observation(revision=6)

    with pytest.raises(ValueError, match="backwards"):
        service.run(BackgroundWorkKind.SELF_TEST, lambda: "forbidden")

    assert resources.calls == 1


def test_same_presence_revision_cannot_change_authoritative_fact() -> None:
    resources = ResourceReader(capacity())
    service, observer = guard(observation(revision=7), resources)

    assert service.preview(BackgroundWorkKind.SELF_TEST).allowed is True
    observer.observation = observation(OwnerPresence.ACTIVE, revision=7)

    with pytest.raises(ValueError, match="reused"):
        service.run(BackgroundWorkKind.SELF_TEST, lambda: "forbidden")

    assert resources.calls == 1


@pytest.mark.parametrize("kind", ["coroutine", "generator"])
def test_deferred_effect_functions_are_rejected_before_authority_reads(kind: str) -> None:
    resources = ResourceReader(capacity())
    service, observer = guard(observation(), resources)

    async def async_effect() -> object:
        return "later"

    def generator_effect():
        yield "later"

    effect = async_effect if kind == "coroutine" else generator_effect
    with pytest.raises(TypeError, match="synchronously"):
        service.run(BackgroundWorkKind.SELF_TEST, effect)

    assert observer.calls == 0
    assert resources.calls == 0
