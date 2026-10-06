from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from nika_core.product_factory_build_execution import BuildExecutionState
from nika_core.product_factory_packaged_build_pass import (
    PackagedBuildPassError,
    _advance_one,
)


def _record(state: BuildExecutionState):
    return SimpleNamespace(
        spec=SimpleNamespace(
            request=SimpleNamespace(work_id="build-work"),
        ),
        state=state,
        block_reason=None,
    )


class _Host:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.record = _record(BuildExecutionState.PENDING)

    def prepare(self, work_id: str):
        assert work_id == "build-work"
        self.calls.append("prepare")
        self.record = _record(BuildExecutionState.PREPARED)
        return self.record

    def begin_dispatch(self, work_id: str):
        assert work_id == "build-work"
        self.calls.append("begin_dispatch")
        self.record = _record(BuildExecutionState.DISPATCHING)
        return SimpleNamespace(dispatch_id="dispatch")

    def execute(self, work_id: str):
        assert work_id == "build-work"
        self.calls.append("execute")
        self.record = _record(BuildExecutionState.SUCCEEDED)
        return self.record

    def reconcile(self, work_id: str):
        assert work_id == "build-work"
        self.calls.append("reconcile")
        return self.record


def test_advance_one_runs_pending_work_through_exact_dispatch_once() -> None:
    host = _Host()

    result = _advance_one(cast(Any, host), cast(Any, host.record))

    assert result.state is BuildExecutionState.SUCCEEDED
    assert host.calls == ["prepare", "begin_dispatch", "execute"]


def test_advance_one_never_replays_reconcile_required_dispatch() -> None:
    host = _Host()
    host.record = _record(BuildExecutionState.RECONCILE_REQUIRED)

    result = _advance_one(cast(Any, host), cast(Any, host.record))

    assert result.state is BuildExecutionState.RECONCILE_REQUIRED
    assert host.calls == ["reconcile"]


@pytest.mark.parametrize(
    "waiting_state",
    [
        BuildExecutionState.WAITING_FOR_NODE,
        BuildExecutionState.WAITING_FOR_AUTHORITY,
    ],
)
def test_advance_one_reports_waiting_state_without_retry_loop(
    waiting_state: BuildExecutionState,
) -> None:
    host = _Host()

    def still_waiting(work_id: str):
        assert work_id == "build-work"
        host.calls.append("prepare")
        return _record(waiting_state)

    host.prepare = still_waiting  # type: ignore[method-assign]
    result = _advance_one(cast(Any, host), cast(Any, _record(waiting_state)))

    assert result.state is waiting_state
    assert host.calls == ["prepare"]


def test_advance_one_has_hard_transition_budget() -> None:
    host = _Host()

    def no_progress(work_id: str):
        assert work_id == "build-work"
        host.calls.append("prepare")
        return _record(BuildExecutionState.PENDING)

    host.prepare = no_progress  # type: ignore[method-assign]

    with pytest.raises(PackagedBuildPassError, match="transition budget"):
        _advance_one(cast(Any, host), cast(Any, host.record))

    assert host.calls == ["prepare"] * 6


def test_advance_one_rejects_unknown_state_without_effect() -> None:
    host = _Host()
    malformed = _record(BuildExecutionState.PENDING)
    malformed.state = cast(Any, "future_state")

    with pytest.raises(PackagedBuildPassError, match="unsupported durable PF5 state"):
        _advance_one(cast(Any, host), cast(Any, malformed))

    assert host.calls == []
