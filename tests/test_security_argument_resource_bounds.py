from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.security import ActionIntent, ExecutionBudget, ExecutionBudgetLedger, SandboxPolicy
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


@pytest.mark.parametrize("malformed", (123, True, b"artifacts/report.txt", ["report.txt"]))
def test_non_text_intent_path_and_executable_fail_at_admission(malformed: object) -> None:
    valid = _intent({})
    with pytest.raises(ValueError, match="workspace-relative"):
        replace(valid, write_path=malformed)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="process executable must be text"):
        replace(valid, executable=malformed)  # type: ignore[arg-type]


@pytest.mark.parametrize("malformed", (123, True, b"artifacts/report.txt", ["report.txt"]))
def test_sandbox_denies_non_text_host_path_and_executable(
    tmp_path: Path, malformed: object
) -> None:
    sandbox = SandboxPolicy(
        workspace_root=tmp_path,
        writable_roots=("artifacts",),
        allowed_network_hosts=("example.test",),
        allowed_executables=("python.exe",),
    )
    with pytest.raises(PermissionError, match="workspace-relative"):
        sandbox.resolve_write(malformed)  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="network host is not allowed"):
        sandbox.authorize_network(malformed)  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="process executable is not allowed"):
        sandbox.authorize_executable(malformed)  # type: ignore[arg-type]


def test_sandbox_config_denies_non_text_allowlist_carriers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allowed network host must be text"):
        SandboxPolicy(
            workspace_root=tmp_path,
            allowed_network_hosts=(123,),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="process executable must be text"):
        SandboxPolicy(
            workspace_root=tmp_path,
            allowed_executables=(123,),  # type: ignore[arg-type]
        )


def test_sandbox_direct_network_admission_denies_bad_unicode(tmp_path: Path) -> None:
    sandbox = SandboxPolicy(workspace_root=tmp_path, allowed_network_hosts=("example.test",))
    with pytest.raises(PermissionError, match="network host is not allowed"):
        sandbox.authorize_network(chr(0xD800))


@pytest.mark.parametrize("field", ("write_bytes", "network_calls", "process_launches"))
@pytest.mark.parametrize("invalid", (True, -1, 1.5, float("nan"), float("inf"), "1"))
def test_mutable_budget_counters_fail_closed_at_reservation(
    field: str, invalid: object
) -> None:
    ledger = ExecutionBudgetLedger(
        ExecutionBudget(max_write_bytes=5, max_network_calls=5, max_process_launches=5)
    )
    setattr(ledger, field, invalid)
    with pytest.raises(ValueError, match="budget usage counters"):
        ledger.reserve(_intent({}))
    for other in ("write_bytes", "network_calls", "process_launches"):
        if other != field:
            assert getattr(ledger, other) == 0


def test_budget_carrier_must_remain_canonical_at_reservation() -> None:
    ledger = ExecutionBudgetLedger(ExecutionBudget())
    ledger.budget = object()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="canonical ExecutionBudget"):
        ledger.reserve(_intent({}))
