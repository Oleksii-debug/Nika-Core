from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from nika_core.ui.bridge_models import UICommand
from nika_core.ui.payload_safety import validate_ui_payload


def command(payload: object) -> UICommand:
    return UICommand.model_validate(
        {"request_id": "request-1", "action_id": "task.create", "payload": payload}
    )


def test_regular_keyboard_command_and_nested_json_remain_accepted() -> None:
    payload = {
        "command": "Створи доступний застосунок 📁",
        "metadata": {"offline": True, "retry": 0, "timeout": 1.5, "optional": None},
        "inputs": ["Дані", False, 42, ["ok"]],
    }
    assert command(payload).payload == payload
    assert command({}).payload == {}
    assert UICommand(request_id="1", action_id="nav.tasks").payload == {}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "JSON object"),
        ([], "JSON object"),
        ({"x": b"bytes"}, "non-JSON"),
        ({1: "value"}, "non-text key"),
        ({"x": ("tuple",)}, "non-JSON"),
        ({"x": {1, 2}}, "non-JSON"),
        ({"x": float("nan")}, "non-finite"),
        ({"x": float("inf")}, "non-finite"),
        ({"x": -float("inf")}, "non-finite"),
        ({"x": 1 << 4096}, "oversized number"),
        ({"x": "\ud800"}, "invalid Unicode"),
    ],
)
def test_malformed_payload_is_rejected_before_handler(payload: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        command(payload)


def test_string_subclass_is_not_executed_to_coerce_bridge_input() -> None:
    class Dangerous(str):
        def __str__(self) -> str:
            raise AssertionError("hostile string coercion")

    with pytest.raises(ValidationError, match="non-JSON"):
        command({"command": Dangerous("hello")})


def test_dict_and_list_cycles_fail_without_recursive_traversal() -> None:
    value: dict[str, object] = {}
    value["again"] = value
    with pytest.raises(ValueError, match="cycle"):
        validate_ui_payload(value)

    sequence: list[object] = []
    sequence.append(sequence)
    with pytest.raises(ValueError, match="cycle"):
        validate_ui_payload({"sequence": sequence})


def test_deeply_nested_payload_is_bounded_before_recursion() -> None:
    value: object = "leaf"
    for _ in range(33):
        value = [value]
    with pytest.raises(ValidationError, match="nesting limit"):
        command({"root": value})


def test_wide_payload_bounds_total_elements_without_copying() -> None:
    assert len(command({"items": [None] * 9_997}).payload["items"]) == 9_997
    with pytest.raises(ValidationError, match="element limit"):
        command({"items": [None] * 9_998})


def test_utf8_byte_budget_includes_structure_and_multibyte_text() -> None:
    # '{"text":"..."}' uses 11 bytes of compact JSON framing.
    allowed = 1_048_576 - len(b'{"text":""}')
    assert command({"text": "x" * allowed}).payload["text"] == "x" * allowed
    with pytest.raises(ValidationError, match="byte limit"):
        command({"text": "x" * (allowed + 1)})
    with pytest.raises(ValidationError, match="byte limit"):
        command({"text": "🧭" * (allowed // 4 + 1)})


def test_utf8_budget_counts_json_escape_expansion_across_values() -> None:
    with pytest.raises(ValidationError, match="byte limit"):
        command({"first": "\x00" * 200_000})
    with pytest.raises(ValidationError, match="byte limit"):
        command({"first": "x" * 600_000, "second": "y" * 600_000})


def test_reused_noncyclic_container_counts_each_occurrence() -> None:
    same = ["ok"]
    assert command({"first": same, "second": same}).payload == {
        "first": ["ok"],
        "second": ["ok"],
    }


def test_valid_float_extremes_and_boundary_integer() -> None:
    payload = {"max_float": math.nextafter(float("inf"), 0.0), "number": (1 << 4095)}
    assert command(payload).payload == payload

def test_admitted_command_detaches_nested_caller_containers() -> None:
    shared = {"steps": ["safe", {"attempts": 2}]}
    original = {"first": shared, "second": shared}
    accepted = command(original)

    assert accepted.payload == original
    assert accepted.payload is not original
    assert accepted.payload["first"] is not shared
    assert accepted.payload["first"] is not accepted.payload["second"]

    original["first"]["steps"].append({"unvalidated": object()})
    original["second"]["steps"][1]["attempts"] = float("nan")
    original["extra"] = {"huge": "x" * 1_048_576}

    assert accepted.payload == {
        "first": {"steps": ["safe", {"attempts": 2}]},
        "second": {"steps": ["safe", {"attempts": 2}]},
    }
