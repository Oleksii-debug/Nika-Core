"""Plan 1 Section 2: runtime port envelopes fail closed on wrong container types.

These checks exercise production Nika-owned dataclass contracts, not a mock
runtime or a duplicated serialization/authority layer.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from nika_core.runtime.contracts import (
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
)


@pytest.mark.parametrize("invalid", [None, 1, True, [], (), "payload", object()])
def test_request_rejects_nonmapping_payload_before_runtime_dispatch(invalid: object) -> None:
    with pytest.raises(TypeError, match="request payload must be a mapping"):
        RuntimeRequest(task_id="task", thread_id="thread", payload=invalid)


@pytest.mark.parametrize("invalid", [None, 1, True, [], (), "payload", object()])
def test_event_rejects_nonmapping_payload_before_durable_evidence(invalid: object) -> None:
    with pytest.raises(TypeError, match="event payload must be a mapping"):
        RuntimeEvent(sequence=0, event_type="runtime.started", payload=invalid)


@pytest.mark.parametrize("invalid", [None, 1, True, [], (), "result", object()])
def test_result_rejects_nonmapping_output_before_readback(invalid: object) -> None:
    with pytest.raises(TypeError, match="runtime output must be a mapping"):
        RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output=invalid)


@pytest.mark.parametrize("invalid", [None, [], [RuntimeEvent(0, "done")], "events", 42])
def test_result_rejects_non_tuple_event_container(invalid: object) -> None:
    with pytest.raises(TypeError, match="tuple of RuntimeEvent"):
        RuntimeResult(outcome=RuntimeOutcome.COMPLETED, events=invalid)


@pytest.mark.parametrize("invalid", [None, object(), {"sequence": 0}, "completed", 1])
def test_result_rejects_non_event_members(invalid: object) -> None:
    with pytest.raises(TypeError, match="tuple of RuntimeEvent"):
        RuntimeResult(outcome=RuntimeOutcome.COMPLETED, events=(invalid,))


def test_empty_tuple_is_the_valid_default_event_container() -> None:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
    assert result.events == ()
    assert result.output == {}


def test_valid_tuple_and_readonly_mapping_remain_compatible() -> None:
    request = RuntimeRequest(
        task_id="task",
        thread_id="thread",
        payload=MappingProxyType({"goal": "readback"}),
    )
    event = RuntimeEvent(
        sequence=0,
        event_type="runtime.completed",
        payload=MappingProxyType({"task_id": request.task_id}),
    )
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(event,),
        output=MappingProxyType({"ok": True}),
    )
    assert result.output["ok"] is True
    assert result.events[0].payload["task_id"] == "task"
