"""Plan 1 Section 2: runtime object-map keys must retain identity across JSON transport."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from nika_core.runtime.contracts import (
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
)


@pytest.mark.parametrize("invalid_key", [1, False, None, (1,), object()])
def test_request_rejects_nonstring_mapping_keys(invalid_key: object) -> None:
    with pytest.raises(TypeError, match="request payload keys must be plain strings"):
        RuntimeRequest("task", "thread", payload={invalid_key: "value"})


@pytest.mark.parametrize("invalid_key", [1, False, None, (1,), object()])
def test_event_rejects_nonstring_mapping_keys(invalid_key: object) -> None:
    with pytest.raises(TypeError, match="event payload keys must be plain strings"):
        RuntimeEvent(0, "runtime.completed", payload={invalid_key: "value"})


@pytest.mark.parametrize("invalid_key", [1, False, None, (1,), object()])
def test_result_rejects_nonstring_mapping_keys(invalid_key: object) -> None:
    with pytest.raises(TypeError, match="runtime output keys must be plain strings"):
        RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={invalid_key: "value"},
        )


def test_numeric_and_text_keys_cannot_merge_during_json_transport() -> None:
    with pytest.raises(TypeError, match="keys must be plain strings"):
        RuntimeRequest("task", "thread", payload={1: "numeric", "1": "text"})


def test_valid_plain_string_keys_and_readonly_mappings_survive() -> None:
    request = RuntimeRequest(
        "task", "thread", payload=MappingProxyType({"": "empty-key", "1": 1})
    )
    event = RuntimeEvent(0, "runtime.completed", payload={"ok": True})
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(event,),
        output=MappingProxyType({"result": request.payload[""]}),
    )
    assert result.output["result"] == "empty-key"
    assert result.events[0].payload["ok"] is True
