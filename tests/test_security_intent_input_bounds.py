from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.security import (
    ActionIntent,
    ApprovalAuthority,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
)
from nika_core.tools import ToolRisk, tool_arguments_fingerprint

BAD_UNICODE = chr(0xD800)


def _intent(**changes: object) -> ActionIntent:
    data: dict[str, object] = {
        "action_id": "effect-1",
        "tool_id": "files.write",
        "risk": ToolRisk.LOCAL_WRITE,
        "target": "Звіт",
        "task_id": "task-1",
        "project_id": "project-1",
        "effect_id": "effect-1",
    }
    data.update(changes)
    return ActionIntent(**data)  # type: ignore[arg-type]


@pytest.mark.parametrize("risk", (ToolRisk.HIGH_IMPACT.value, "HIGH_IMPACT", "unknown", 1, None))
def test_risk_carrier_must_be_canonical_enum(risk: object) -> None:
    with pytest.raises(ValueError, match="ToolRisk"):
        _intent(risk=risk)


@pytest.mark.parametrize("approval_required", ("false", "true", 0, 1, None))
def test_approval_flag_must_be_a_real_boolean(approval_required: object) -> None:
    with pytest.raises(ValueError, match="boolean"):
        _intent(approval_required=approval_required)


def test_valid_explicit_approval_flag_retains_existing_semantics() -> None:
    assert not _intent(approval_required=False).requires_approval
    assert _intent(approval_required=True).requires_approval


@pytest.mark.parametrize("field", ("max_write_bytes", "max_network_calls", "max_process_launches"))
@pytest.mark.parametrize("invalid", (True, 1.5, float("nan"), float("inf"), "10", -1))
def test_execution_budget_rejects_non_integral_or_unbounded_limits(
    field: str, invalid: object
) -> None:
    with pytest.raises(ValueError, match="non-negative integers"):
        ExecutionBudget(**{field: invalid})


@pytest.mark.parametrize("invalid", (True, False, 1.5, float("nan"), float("inf"), "10", -1))
def test_write_intent_rejects_non_integral_bytes_before_reservation(invalid: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        _intent(write_path="artifacts/result.txt", write_bytes=invalid)


def test_valid_integer_budget_remains_atomic_at_boundary() -> None:
    ledger = ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=3))
    ledger.reserve(_intent(write_path="artifacts/one.txt", write_bytes=3))
    with pytest.raises(PermissionError, match="write budget"):
        ledger.reserve(_intent(write_path="artifacts/two.txt", write_bytes=1))
    assert ledger.write_bytes == 3
    assert ledger.network_calls == 0
    assert ledger.process_launches == 0


@pytest.mark.parametrize(
    "changes",
    (
        {"action_id": BAD_UNICODE},
        {"tool_id": BAD_UNICODE},
        {"target": BAD_UNICODE},
        {"task_id": BAD_UNICODE},
        {"project_id": BAD_UNICODE},
        {"site": BAD_UNICODE},
        {"resource": BAD_UNICODE},
        {"effect_id": BAD_UNICODE},
        {"authority_version": BAD_UNICODE},
        {"network_host": BAD_UNICODE},
        {"scope": (("account", BAD_UNICODE),)},
        {"scope": ((BAD_UNICODE, "demo"),)},
        {"write_path": "artifacts/" + BAD_UNICODE},
        {"executable": BAD_UNICODE},
    ),
)
def test_unencodable_intent_carriers_fail_before_fingerprints(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        _intent(**changes)


@pytest.mark.parametrize(
    "arguments",
    (
        {"title": BAD_UNICODE},
        {"nested": {"title": BAD_UNICODE}},
        {BAD_UNICODE: "value"},
    ),
)
def test_unencodable_argument_values_and_keys_fail_at_admission(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="deterministic JSON-compatible"):
        _intent(arguments=arguments)


@pytest.mark.parametrize(
    "arguments",
    (
        {"title": BAD_UNICODE},
        {"nested": {"title": BAD_UNICODE}},
        {BAD_UNICODE: "value"},
    ),
)
def test_direct_tool_argument_fingerprint_rejects_unencodable_text(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        tool_arguments_fingerprint(arguments)


def test_sandbox_rejects_unencodable_paths_and_executables(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        SandboxPolicy(workspace_root=tmp_path, writable_roots=("artifacts/" + BAD_UNICODE,))
    with pytest.raises(ValueError, match="UTF-8"):
        SandboxPolicy(workspace_root=tmp_path, allowed_executables=(BAD_UNICODE,))
    sandbox = SandboxPolicy(
        workspace_root=tmp_path,
        writable_roots=("artifacts",),
        allowed_executables=("python.exe",),
    )
    with pytest.raises(PermissionError, match="UTF-8"):
        sandbox.resolve_write("artifacts/" + BAD_UNICODE)
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable(BAD_UNICODE)


def test_valid_unicode_identity_and_approval_continue_to_work() -> None:
    intent = _intent(
        risk=ToolRisk.HIGH_IMPACT,
        target="Звіт ✅",
        arguments={"e\u0301": ["Київ", "😀"]},
        scope=(("проєкт", "Ніка"),),
    )
    same = _intent(
        risk=ToolRisk.HIGH_IMPACT,
        target="Звіт ✅",
        arguments={"é": ["Київ", "😀"]},
        scope=(("проєкт", "Ніка"),),
    )
    assert intent.arguments_fingerprint == tool_arguments_fingerprint({"é": ["Київ", "😀"]})
    assert intent.effect_fingerprint == same.effect_fingerprint
    assert intent.approval_fingerprint == same.approval_fingerprint
    authority = ApprovalAuthority(secret=b"test-only-no-production-secret-123456789")
    now = datetime(2026, 10, 4, tzinfo=UTC)
    view = authority.request(intent, now=now)
    evidence = authority.approve(view.request_id, now=now)
    authority.verifier().validate_locked(intent, evidence, now=now)
