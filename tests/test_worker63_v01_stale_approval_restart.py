from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.security import (
    ActionIntent,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolCall, ToolExecutor, ToolRisk, ToolSpec


def _policy(tmp_path: Path, *, widened: bool) -> SecurityPolicy:
    granted_tools = {"danger.execute"}
    if widened:
        granted_tools.add("auxiliary.read")
    return SecurityPolicy(
        granted_tools=frozenset(granted_tools),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            writable_roots=("artifacts",),
            allowed_network_hosts=(),
            allowed_executables=(),
        ),
        budget=ExecutionBudget(
            max_write_bytes=0,
            max_network_calls=0,
            max_process_launches=0,
        ),
    )


def test_consumed_approval_cannot_rebind_after_restart_and_permission_ceiling_change(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 20, 30, tzinfo=UTC)
    intent = ActionIntent(
        action_id="worker63-controlled-effect",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="controlled-resource",
        approval_required=True,
    )
    approval = ApprovalEvidence(
        approval_id="worker63-approval-1",
        action_fingerprint=intent.approval_fingerprint,
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
    )

    original_policy = _policy(tmp_path, widened=False)
    original_ledger = ApprovalLedger()
    authorize_action(
        intent,
        original_policy,
        ExecutionBudgetLedger(original_policy.budget),
        original_ledger,
        approval=approval,
        now=now,
    )
    with pytest.raises(PermissionError, match="already used"):
        authorize_action(
            intent,
            original_policy,
            ExecutionBudgetLedger(original_policy.budget),
            original_ledger,
            approval=approval,
            now=now,
        )

    restarted_policy = _policy(tmp_path, widened=True)
    assert restarted_policy.sandbox == original_policy.sandbox
    assert restarted_policy.budget == original_policy.budget
    assert original_policy.granted_tools == frozenset({"danger.execute"})
    assert restarted_policy.granted_tools == frozenset({"danger.execute", "auxiliary.read"})

    stale_after_restart = ApprovalEvidence(
        approval_id=approval.approval_id,
        action_fingerprint=approval.action_fingerprint,
        approved_at=approval.approved_at,
        expires_at=approval.expires_at,
    )
    restarted_approval_ledger = ApprovalLedger()
    restarted_budget_ledger = ExecutionBudgetLedger(restarted_policy.budget)

    store = SQLiteStore(tmp_path / "worker63-audit.db")
    store.initialize()
    audit_log = AuditLog(store)
    handler_invocations = 0

    async def approval_policy(_spec: ToolSpec, _call: ToolCall) -> bool:
        try:
            authorize_action(
                intent,
                restarted_policy,
                restarted_budget_ledger,
                restarted_approval_ledger,
                approval=stale_after_restart,
                now=now + timedelta(seconds=1),
            )
        except PermissionError:
            return False
        return True

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal handler_invocations
        handler_invocations += 1
        return {"accepted": bool(arguments)}

    executor = ToolExecutor(audit_log=audit_log, approval_policy=approval_policy)
    executor.register(
        ToolSpec(
            tool_id="danger.execute",
            description="Worker63 controlled high-impact effect",
            risk=ToolRisk.HIGH_IMPACT,
        ),
        handler,
    )

    sensitive_canary = "worker63-sensitive-canary"
    result = asyncio.run(
        executor.execute(
            ToolCall(
                call_id="worker63-restarted-call",
                tool_id="danger.execute",
                arguments={"payload": sensitive_canary},
                approved=False,
            )
        )
    )

    events = audit_log.list_for(
        entity_type="tool_call",
        entity_id="worker63-restarted-call",
    )
    event_types = tuple(event.event_type for event in events)
    serialized_audit = json.dumps(
        [event.payload for event in events],
        ensure_ascii=False,
        sort_keys=True,
    )

    observed = (
        handler_invocations,
        result.error,
        event_types,
        sensitive_canary not in serialized_audit,
    )
    expected = (
        0,
        "approval required",
        ("tool.denied",),
        True,
    )
    assert observed == expected
