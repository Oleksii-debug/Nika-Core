"""Thin typed page-readiness classification over canonical semantic interaction contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from math import ceil, isfinite
from time import monotonic

from nika_core.interaction.domain import (
    AmbiguousTargetError,
    ControlLocator,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
)
from nika_core.interaction.resolver import resolve_strict, validate_snapshot


class PageReadinessState(StrEnum):
    LOADING = "LOADING"
    TEMPORARILY_BUSY = "TEMPORARILY_BUSY"
    CONTROL_DISABLED_TRANSIENT = "CONTROL_DISABLED_TRANSIENT"
    READY = "READY"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"
    PAGE_STALE = "PAGE_STALE"
    PAGE_CLOSED = "PAGE_CLOSED"
    VALIDATION_ERROR = "VALIDATION_ERROR"


class PageObservationSignal(StrEnum):
    """Typed observation supplied by the page adapter/workflow, never inferred from text."""

    ACTIVE = "active"
    LOADING = "loading"
    TEMPORARILY_BUSY = "temporarily_busy"
    CONTROL_DISABLED_TRANSIENT = "control_disabled_transient"
    PAGE_CLOSED = "page_closed"


_RETRY_CANDIDATES = frozenset(
    {
        PageReadinessState.LOADING,
        PageReadinessState.TEMPORARILY_BUSY,
        PageReadinessState.CONTROL_DISABLED_TRANSIENT,
    }
)


@dataclass(frozen=True, slots=True)
class PageReadinessResult:
    state: PageReadinessState
    reason: str
    observations: int = 1
    observation_window_exhausted: bool = False

    @property
    def retry_candidate(self) -> bool:
        """Whether higher-level policy may consider a bounded replay-safe retry."""
        return self.state in _RETRY_CANDIDATES


def _result(state: PageReadinessState, reason: str) -> PageReadinessResult:
    return PageReadinessResult(state=state, reason=reason)


def classify_page_readiness(
    *,
    snapshot: SemanticSnapshot | None,
    locator: ControlLocator | None,
    signal: PageObservationSignal = PageObservationSignal.ACTIVE,
    expected_snapshot: SemanticSnapshot | None = None,
) -> PageReadinessResult:
    """Classify one observation using canonical strict semantic resolution.

    Transient states must arrive as typed signals. Resolver failures are mapped by
    exception type only; exception messages are deliberately ignored.
    """
    if not isinstance(signal, PageObservationSignal):
        return _result(PageReadinessState.VALIDATION_ERROR, "invalid page observation signal")

    if signal == PageObservationSignal.PAGE_CLOSED:
        return _result(PageReadinessState.PAGE_CLOSED, "page is closed")
    if signal == PageObservationSignal.LOADING:
        return _result(PageReadinessState.LOADING, "page reports loading")
    if signal == PageObservationSignal.TEMPORARILY_BUSY:
        return _result(PageReadinessState.TEMPORARILY_BUSY, "page reports temporary busy state")

    if not isinstance(snapshot, SemanticSnapshot) or not isinstance(locator, ControlLocator):
        return _result(
            PageReadinessState.VALIDATION_ERROR,
            "active semantic classification requires snapshot and locator",
        )
    if expected_snapshot is not None and not isinstance(expected_snapshot, SemanticSnapshot):
        return _result(PageReadinessState.VALIDATION_ERROR, "expected snapshot is invalid")

    if expected_snapshot is not None:
        try:
            validate_snapshot(expected_snapshot, snapshot)
        except StaleSnapshotError:
            return _result(PageReadinessState.PAGE_STALE, "semantic snapshot is stale")

    try:
        control = resolve_strict(snapshot, locator)
    except TargetNotFoundError:
        return _result(PageReadinessState.MISSING, "semantic target is missing")
    except AmbiguousTargetError:
        return _result(PageReadinessState.AMBIGUOUS, "semantic target is ambiguous")
    except StaleSnapshotError:
        return _result(PageReadinessState.PAGE_STALE, "semantic snapshot is stale")

    if signal == PageObservationSignal.CONTROL_DISABLED_TRANSIENT:
        if control.enabled:
            return _result(
                PageReadinessState.VALIDATION_ERROR,
                "transient-disabled signal conflicts with enabled control",
            )
        return _result(
            PageReadinessState.CONTROL_DISABLED_TRANSIENT,
            "control is explicitly disabled transiently",
        )

    if not control.visible:
        return _result(PageReadinessState.MISSING, "semantic target is not visible")
    if not control.enabled:
        return _result(
            PageReadinessState.VALIDATION_ERROR,
            "disabled control lacks explicit transient classification",
        )
    return _result(PageReadinessState.READY, "semantic target is ready")


def _validate_window(timeout_seconds: float, poll_interval_seconds: float) -> tuple[float, float]:
    values = (timeout_seconds, poll_interval_seconds)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("observation window values must be finite positive numbers")
    timeout = float(timeout_seconds)
    interval = float(poll_interval_seconds)
    if not isfinite(timeout) or not isfinite(interval) or timeout <= 0 or interval <= 0:
        raise ValueError("observation window values must be finite positive numbers")
    return timeout, interval


async def observe_page_readiness(
    sample: Callable[[], PageReadinessResult],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    cancellation_event: asyncio.Event | None = None,
    clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PageReadinessResult:
    """Observe readiness within a hard finite window; poll only explicit transient states."""
    timeout, interval = _validate_window(timeout_seconds, poll_interval_seconds)
    if not callable(sample) or not callable(clock) or not callable(sleeper):
        raise ValueError("sample, clock, and sleeper must be callable")
    if cancellation_event is not None and not isinstance(cancellation_event, asyncio.Event):
        raise ValueError("cancellation_event must be an asyncio.Event")

    start = clock()
    if not isinstance(start, (int, float)) or isinstance(start, bool) or not isfinite(float(start)):
        raise ValueError("clock must return a finite monotonic value")
    deadline = float(start) + timeout
    max_observations = ceil(timeout / interval) + 1

    last_result: PageReadinessResult | None = None
    for observation_number in range(1, max_observations + 1):
        if cancellation_event is not None and cancellation_event.is_set():
            raise asyncio.CancelledError

        result = sample()
        if not isinstance(result, PageReadinessResult):
            return PageReadinessResult(
                PageReadinessState.VALIDATION_ERROR,
                "readiness sampler returned an invalid result",
                observations=observation_number,
            )
        last_result = replace(result, observations=observation_number)
        if not last_result.retry_candidate:
            return last_result

        now = clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not isfinite(float(now)):
            raise ValueError("clock must return a finite monotonic value")
        remaining = deadline - float(now)
        if remaining <= 0 or observation_number >= max_observations:
            return replace(last_result, observation_window_exhausted=True)

        await sleeper(min(interval, remaining))
        if cancellation_event is not None and cancellation_event.is_set():
            raise asyncio.CancelledError

    assert last_result is not None
    return replace(last_result, observation_window_exhausted=True)
