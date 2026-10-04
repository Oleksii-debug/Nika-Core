from __future__ import annotations

from collections.abc import Mapping

import pytest

from nika_core.security import ActionIntent
from nika_core.tools import ToolRisk


def _intent(arguments: Mapping[str, object]) -> ActionIntent:
    return ActionIntent(
        action_id="bounded-arguments",
        tool_id="files.write",
        risk=ToolRisk.LOCAL_WRITE,
        target="result",
        arguments=arguments,
    )


@pytest.mark.parametrize("container", ("list", "mapping"))
def test_cyclic_arguments_fail_at_admission_without_recursion_error(container: str) -> None:
    if container == "list":
        cycle: list[object] = []
        cycle.append(cycle)
        arguments: Mapping[str, object] = {"value": cycle}
    else:
        mapping: dict[str, object] = {}
        mapping["loop"] = mapping
        arguments = mapping

    with pytest.raises(ValueError, match="deterministic JSON-compatible") as error:
        _intent(arguments)
    assert error.value.__cause__ is not None
    assert "nesting depth" in str(error.value.__cause__)


def test_deep_acyclic_arguments_fail_before_python_recursion_limit() -> None:
    nested: object = "leaf"
    for _ in range(65):
        nested = [nested]

    with pytest.raises(ValueError, match="deterministic JSON-compatible") as error:
        _intent({"nested": nested})
    assert error.value.__cause__ is not None
    assert "nesting depth" in str(error.value.__cause__)


def test_wide_arguments_fail_before_unbounded_json_materialization() -> None:
    with pytest.raises(ValueError, match="deterministic JSON-compatible") as error:
        _intent({"items": [0] * 20_001})
    assert error.value.__cause__ is not None
    assert "node count" in str(error.value.__cause__)


@pytest.mark.parametrize("kind", ("ascii", "multibyte", "escaped_json"))
def test_oversized_arguments_fail_closed(kind: str) -> None:
    if kind == "ascii":
        value = "x" * (8 * 1024 * 1024 + 1)
    elif kind == "multibyte":
        value = "😀" * (8 * 1024 * 1024 // 4 + 1)
    else:
        # The raw UTF-8 content fits, but JSON control-character escaping does not.
        value = "\u0001" * (8 * 1024 * 1024 // 6 + 1)

    with pytest.raises(ValueError, match="deterministic JSON-compatible") as error:
        _intent({"text": value})
    assert error.value.__cause__ is not None
    assert "byte size" in str(error.value.__cause__)


def test_normal_nested_unicode_arguments_preserve_exact_fingerprints() -> None:
    a = _intent({"names": [{"e\u0301": ["Київ", "😀"]}], "count": 1})
    b = _intent({"names": [{"é": ["Київ", "😀"]}], "count": 1})
    assert a.effect_fingerprint == b.effect_fingerprint
    assert a.approval_fingerprint == b.approval_fingerprint
    assert a.normalized_arguments_json == b.normalized_arguments_json
    assert a.arguments["names"][0]["é"] == ("Київ", "😀")


def test_exact_node_and_depth_boundaries_remain_usable() -> None:
    # Root object + key + array + 19,997 integers = exactly 20,000 nodes.
    assert _intent({"items": [0] * 19_997}).arguments["items"][-1] == 0

    nested: object = "valid"
    for _ in range(63):
        nested = [nested]
    # The leaf is at depth 64; only deeper values must be rejected.
    assert _intent({"nested": nested}).normalized_arguments_json.startswith('{"nested":')


def test_exact_serialized_size_boundary_and_one_byte_overflow() -> None:
    # A single `text` entry adds eleven JSON punctuation/key bytes.
    accepted = "x" * (8 * 1024 * 1024 - 11)
    assert len(_intent({"text": accepted}).normalized_arguments_json) == 8 * 1024 * 1024
    with pytest.raises(ValueError, match="deterministic JSON-compatible") as error:
        _intent({"text": accepted + "x"})
    assert error.value.__cause__ is not None
    assert "byte size" in str(error.value.__cause__)


def test_depth_within_limit_preserves_normal_arguments() -> None:
    nested: object = "valid"
    for _ in range(32):
        nested = [nested]
    assert _intent({"nested": nested}).normalized_arguments_json.startswith('{"nested":')
