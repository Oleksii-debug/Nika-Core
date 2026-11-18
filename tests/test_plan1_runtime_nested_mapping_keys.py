"""Plan 1 §2: nested transport map keys must not silently coalesce in JSON."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from nika_core.runtime.contracts import (
    MAX_RUNTIME_MAPPING_WALK_NODES,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
)


def _request(value: object) -> object:
    return RuntimeRequest("task", "thread", payload=value)


def _event(value: object) -> object:
    return RuntimeEvent(0, "runtime.completed", payload=value)


def _result(value: object) -> object:
    return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output=value)


@pytest.mark.parametrize("construct", [_request, _event, _result])
@pytest.mark.parametrize(
    "nested",
    [
        {"nested": {1: "numeric", "1": "text"}},
        {"items": [{False: "false", "False": "text"}]},
        {"items": ({None: "null"},)},
        {"readonly": MappingProxyType({2: "invalid"})},
    ],
)
def test_nonstring_keys_in_nested_json_objects_fail_closed(construct, nested) -> None:
    with pytest.raises(TypeError, match="keys must be plain strings"):
        construct(nested)


@pytest.mark.parametrize("construct", [_request, _event, _result])
def test_cyclic_transport_containers_fail_before_serialization(construct) -> None:
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cyclic container"):
        construct({"items": cycle})


@pytest.mark.parametrize("construct", [_request, _event, _result])
def test_shared_subtrees_and_readonly_nested_maps_remain_compatible(construct) -> None:
    shared = MappingProxyType({"field": 1})
    value = {"items": [shared, shared], "other": (shared,)}
    assert construct(value) is not None


@pytest.mark.parametrize("construct", [_request, _event, _result])
def test_bounded_inspection_rejects_large_nested_carriers(construct) -> None:
    oversized = [0] * (MAX_RUNTIME_MAPPING_WALK_NODES + 1)
    with pytest.raises(ValueError, match="inspection limit"):
        construct({"items": oversized})
