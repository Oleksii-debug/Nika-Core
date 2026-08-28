from __future__ import annotations

import asyncio

import pytest

import nika_core.page_readiness as readiness
from nika_core.interaction.domain import (
    ControlLocator,
    ControlNode,
    InteractionTarget,
    SemanticSnapshot,
)
from nika_core.page_readiness import (
    PageObservationSignal,
    PageReadinessResult,
    PageReadinessState,
    classify_page_readiness,
    observe_page_readiness,
)


def _snapshot(*controls: ControlNode, generation: int = 1, revision: int = 1) -> SemanticSnapshot:
    return SemanticSnapshot(
        target=InteractionTarget(),
        generation=generation,
        revision=revision,
        controls=tuple(controls),
    )


def _button(*, node_id: str = "save", enabled: bool = True, visible: bool = True) -> ControlNode:
    return ControlNode(
        node_id=node_id,
        role="button",
        name="Save",
        enabled=enabled,
        visible=visible,
    )


def test_classifier_covers_all_required_states() -> None:
    locator = ControlLocator(role="button", name="Save")
    ready = _snapshot(_button())

    cases = [
        (
            dict(snapshot=None, locator=None, signal=PageObservationSignal.LOADING),
            PageReadinessState.LOADING,
        ),
        (
            dict(snapshot=None, locator=None, signal=PageObservationSignal.TEMPORARILY_BUSY),
            PageReadinessState.TEMPORARILY_BUSY,
        ),
        (
            dict(
                snapshot=_snapshot(_button(enabled=False)),
                locator=locator,
                signal=PageObservationSignal.CONTROL_DISABLED_TRANSIENT,
            ),
            PageReadinessState.CONTROL_DISABLED_TRANSIENT,
        ),
        (dict(snapshot=ready, locator=locator), PageReadinessState.READY),
        (dict(snapshot=_snapshot(), locator=locator), PageReadinessState.MISSING),
        (
            dict(snapshot=_snapshot(_button(node_id="a"), _button(node_id="b")), locator=locator),
            PageReadinessState.AMBIGUOUS,
        ),
        (
            dict(
                snapshot=ready,
                locator=locator,
                expected_snapshot=_snapshot(_button(), revision=0),
            ),
            PageReadinessState.PAGE_STALE,
        ),
        (
            dict(snapshot=None, locator=None, signal=PageObservationSignal.PAGE_CLOSED),
            PageReadinessState.PAGE_CLOSED,
        ),
        (dict(snapshot=None, locator=locator), PageReadinessState.VALIDATION_ERROR),
    ]

    assert {expected for _, expected in cases} == set(PageReadinessState)
    for kwargs, expected in cases:
        assert classify_page_readiness(**kwargs).state == expected


def test_only_explicit_transient_states_are_retry_candidates() -> None:
    retryable = {
        PageReadinessState.LOADING,
        PageReadinessState.TEMPORARILY_BUSY,
        PageReadinessState.CONTROL_DISABLED_TRANSIENT,
    }
    for state in PageReadinessState:
        result = PageReadinessResult(state, "fixture")
        assert result.retry_candidate is (state in retryable)


def test_disabled_control_is_not_retryable_without_explicit_transient_signal() -> None:
    result = classify_page_readiness(
        snapshot=_snapshot(_button(enabled=False)),
        locator=ControlLocator(role="button", name="Save"),
    )
    assert result.state == PageReadinessState.VALIDATION_ERROR
    assert result.retry_candidate is False


def test_typed_transient_signal_must_match_disabled_control() -> None:
    result = classify_page_readiness(
        snapshot=_snapshot(_button(enabled=True)),
        locator=ControlLocator(role="button", name="Save"),
        signal=PageObservationSignal.CONTROL_DISABLED_TRANSIENT,
    )
    assert result.state == PageReadinessState.VALIDATION_ERROR
    assert result.retry_candidate is False


def test_invisible_control_is_fail_closed_not_automatic_retry() -> None:
    result = classify_page_readiness(
        snapshot=_snapshot(_button(visible=False)),
        locator=ControlLocator(role="button", name="Save"),
    )
    assert result.state == PageReadinessState.MISSING
    assert result.retry_candidate is False


def test_transient_disabled_hint_cannot_override_hidden_control() -> None:
    result = classify_page_readiness(
        snapshot=_snapshot(_button(enabled=False, visible=False)),
        locator=ControlLocator(role="button", name="Save"),
        signal=PageObservationSignal.CONTROL_DISABLED_TRANSIENT,
    )
    assert result.state == PageReadinessState.MISSING
    assert result.retry_candidate is False


def test_exception_text_is_not_used_as_readiness_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    def arbitrary_failure(*args: object, **kwargs: object) -> ControlNode:
        raise RuntimeError("temporarily busy page closed ambiguous loading")

    monkeypatch.setattr(readiness, "resolve_strict", arbitrary_failure)
    with pytest.raises(RuntimeError, match="temporarily busy"):
        classify_page_readiness(
            snapshot=_snapshot(_button()),
            locator=ControlLocator(role="button", name="Save"),
        )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def test_observation_window_is_bounded_without_infinite_polling() -> None:
    clock = _FakeClock()
    calls = 0

    def sample() -> PageReadinessResult:
        nonlocal calls
        calls += 1
        return PageReadinessResult(PageReadinessState.LOADING, "fixture")

    result = asyncio.run(
        observe_page_readiness(
            sample,
            timeout_seconds=0.25,
            poll_interval_seconds=0.1,
            clock=clock,
            sleeper=clock.sleep,
        )
    )

    assert result.state == PageReadinessState.LOADING
    assert result.retry_candidate is True
    assert result.observation_window_exhausted is True
    assert result.observations == 4
    assert calls == 4
    assert clock.now == pytest.approx(0.25)
    assert clock.sleeps == pytest.approx([0.1, 0.1, 0.05])


def test_observation_stops_when_page_becomes_ready() -> None:
    clock = _FakeClock()
    states = iter(
        [
            PageReadinessState.LOADING,
            PageReadinessState.TEMPORARILY_BUSY,
            PageReadinessState.READY,
        ]
    )

    def sample() -> PageReadinessResult:
        return PageReadinessResult(next(states), "fixture")

    result = asyncio.run(
        observe_page_readiness(
            sample,
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            clock=clock,
            sleeper=clock.sleep,
        )
    )

    assert result.state == PageReadinessState.READY
    assert result.observations == 3
    assert result.observation_window_exhausted is False
    assert clock.sleeps == [0.1, 0.1]


def test_cancellation_stops_before_another_observation() -> None:
    clock = _FakeClock()
    cancelled = asyncio.Event()
    calls = 0

    def sample() -> PageReadinessResult:
        nonlocal calls
        calls += 1
        return PageReadinessResult(PageReadinessState.LOADING, "fixture")

    async def sleep_and_cancel(delay: float) -> None:
        await clock.sleep(delay)
        cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            observe_page_readiness(
                sample,
                timeout_seconds=1.0,
                poll_interval_seconds=0.1,
                cancellation_event=cancelled,
                clock=clock,
                sleeper=sleep_and_cancel,
            )
        )

    assert calls == 1


def test_non_transient_failure_is_not_polled() -> None:
    clock = _FakeClock()
    calls = 0

    def sample() -> PageReadinessResult:
        nonlocal calls
        calls += 1
        return PageReadinessResult(PageReadinessState.AMBIGUOUS, "fixture")

    result = asyncio.run(
        observe_page_readiness(
            sample,
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            clock=clock,
            sleeper=clock.sleep,
        )
    )

    assert result.state == PageReadinessState.AMBIGUOUS
    assert result.retry_candidate is False
    assert calls == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("timeout", "interval"),
    [(0, 0.1), (-1, 0.1), (1, 0), (float("inf"), 0.1), (1, float("nan"))],
)
def test_observation_window_rejects_invalid_bounds(timeout: float, interval: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        asyncio.run(
            observe_page_readiness(
                lambda: PageReadinessResult(PageReadinessState.READY, "fixture"),
                timeout_seconds=timeout,
                poll_interval_seconds=interval,
            )
        )
