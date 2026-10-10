from __future__ import annotations

import asyncio

import pytest

from nika_core.page_readiness import (
    PageReadinessResult,
    PageReadinessState,
    observe_page_readiness,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def test_oversleep_does_not_authorize_post_deadline_sample() -> None:
    clock = _Clock()
    calls = 0

    def sample() -> PageReadinessResult:
        nonlocal calls
        calls += 1
        state = PageReadinessState.LOADING if calls == 1 else PageReadinessState.READY
        return PageReadinessResult(state, "fixture")

    async def oversleep(delay: float) -> None:
        clock.sleeps.append(delay)
        clock.now += delay + 0.1

    result = asyncio.run(
        observe_page_readiness(
            sample,
            timeout_seconds=0.1,
            poll_interval_seconds=0.1,
            clock=clock,
            sleeper=oversleep,
        )
    )

    assert result.state is PageReadinessState.LOADING
    assert result.observation_window_exhausted is True
    assert result.observations == 1
    assert calls == 1
    assert clock.now == pytest.approx(0.2)


def test_exact_deadline_sample_remains_allowed() -> None:
    clock = _Clock()
    states = iter([PageReadinessState.LOADING, PageReadinessState.READY])
    calls = 0

    def sample() -> PageReadinessResult:
        nonlocal calls
        calls += 1
        return PageReadinessResult(next(states), "fixture")

    result = asyncio.run(
        observe_page_readiness(
            sample,
            timeout_seconds=0.1,
            poll_interval_seconds=0.1,
            clock=clock,
            sleeper=clock.sleep,
        )
    )

    assert result.state is PageReadinessState.READY
    assert result.observation_window_exhausted is False
    assert result.observations == 2
    assert calls == 2
    assert clock.now == pytest.approx(0.1)
