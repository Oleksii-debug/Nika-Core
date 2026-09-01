from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.security.policy import (
    ActionIntent,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec


APPROVED_AT = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SWAP_CANARY_B = "WORKER64_ARGUMENT_B_CANARY_NOT_FOR_AUDIT"


def _spec() -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="Worker64 post-approval argument-swap oracle",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        timeout_seconds=30.0,
    )


def _call(
    *,
    task_id: str,
    call_id: str,
    arguments: dict[str, object],
    approved: bool,
) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_id="publish",
        task_id=task_id,
        arguments=arguments,
        approved=approved,
    )


def _canonical_effect_fingerprint(arguments: dict[str, object]) -> str:
    """Reuse the exact #558 canonical JSON normalization instead of inventing test normalization."""
    return ToolEffectGuard._fingerprint(
        spec=_spec(),
        call=_call(
            task_id="identity-only",
            call_id="identity-only",
            arguments=arguments,
            approved=False,
        ),
    )


def _intent(arguments: dict[str, object]) -> ActionIntent:
    """Bind current canonical effect identity into current ActionIntent's exact-action identity.

    ActionIntent on the exact parent has no generic arguments field. Encoding the already-canonical
    ToolEffectGuard fingerprint into action_id lets this QA compose the two existing contracts
    without introducing a second normalization algorithm or changing production code.
    """
    return ActionIntent(
        action_id=f"publish-message:{_canonical_effect_fingerprint(arguments)}",
        tool_id="publish",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target="account:worker64-fixed-target",
        approval_required=True,
    )


def _approval(intent: ActionIntent, *, approval_id: str) -> ApprovalEvidence:
    return ApprovalEvidence(
        approval_id=approval_id,
        action_fingerprint=intent.approval_fingerprint,
        approved_at=APPROVED_AT,
        expires_at=APPROVED_AT + timedelta(hours=1),
    )


def _policy(root: Path) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"publish"}),
        sandbox=SandboxPolicy(workspace_root=root),
        budget=ExecutionBudget(),
    )


def _authorize(
    *,
    root: Path,
    intent: ActionIntent,
    approval: ApprovalEvidence,
) -> bool:
    decision = authorize_action(
        intent,
        _policy(root),
        ExecutionBudgetLedger(ExecutionBudget()),
        ApprovalLedger(),
        approval=approval,
        now=APPROVED_AT + timedelta(minutes=1),
    )
    return decision.approved


def _runtime(
    database: Path,
    *,
    task_id: str,
) -> tuple[ToolEffectGuard, IdempotencyLedger, AuditLog]:
    store = SQLiteStore(database)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, 'worker64-proof', 'worker64', 'created', '{}', ?, ?)
            """,
            (
                task_id,
                "2026-08-28T00:00:00+00:00",
                "2026-08-28T00:00:00+00:00",
            ),
        )
    ledger = IdempotencyLedger(store)
    return ToolEffectGuard(ledger), ledger, AuditLog(store)


def test_reordered_equivalent_arguments_keep_canonical_approval_identity(
    tmp_path: Path,
) -> None:
    arguments_a = {
        "target": {
            "resource": "alpha",
            "metadata": {"first": 1, "second": 2},
        },
        "payload": {
            "message": "same",
            "options": {"mode": "safe", "priority": 1},
        },
    }
    reordered_a = {
        "payload": {
            "options": {"priority": 1, "mode": "safe"},
            "message": "same",
        },
        "target": {
            "metadata": {"second": 2, "first": 1},
            "resource": "alpha",
        },
    }

    assert _canonical_effect_fingerprint(arguments_a) == _canonical_effect_fingerprint(reordered_a)
    intent_a = _intent(arguments_a)
    reordered_intent = _intent(reordered_a)
    assert intent_a.approval_fingerprint == reordered_intent.approval_fingerprint

    approval = _approval(intent_a, approval_id="worker64-reordered")
    approved = _authorize(root=tmp_path, intent=reordered_intent, approval=approval)
    assert approved

    database = tmp_path / "reordered.db"
    task_id = "worker64-reordered-task"
    guard, ledger, audit = _runtime(database, task_id=task_id)
    calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"status": "canonical-equivalent"}

    executor = ToolExecutor(audit_log=audit, effect_guard=guard)
    executor.register(_spec(), handler)
    result = asyncio.run(
        executor.execute(
            _call(
                task_id=task_id,
                call_id="worker64-reordered-effect",
                arguments=reordered_a,
                approved=approved,
            )
        )
    )

    assert result.ok
    assert calls == 1
    records = ledger.list_for_task(task_id)
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.COMPLETED


def test_material_argument_swap_after_approval_fails_closed_before_handler_and_restart(
    tmp_path: Path,
) -> None:
    arguments_a = {
        "target": {"resource": "alpha", "slot": 1},
        "payload": {
            "message": "approved-A",
            "options": {"mode": "safe", "priority": 1},
        },
    }
    arguments_b = {
        "target": {"resource": "alpha", "slot": 1},
        "payload": {
            "message": SWAP_CANARY_B,
            "options": {"mode": "safe", "priority": 1},
        },
    }

    intent_a = _intent(arguments_a)
    intent_b = _intent(arguments_b)
    assert _canonical_effect_fingerprint(arguments_a) != _canonical_effect_fingerprint(arguments_b)
    assert intent_a.approval_fingerprint != intent_b.approval_fingerprint

    approval_a = _approval(intent_a, approval_id="worker64-approval-A")

    # Canonical security already rejects A's evidence when B is actually presented to it.
    with pytest.raises(PermissionError, match="exact action"):
        authorize_action(
            intent_b,
            _policy(tmp_path),
            ExecutionBudgetLedger(ExecutionBudget()),
            ApprovalLedger(),
            approval=approval_a,
            now=APPROVED_AT + timedelta(minutes=1),
        )

    # Approve exact A. The attack swaps only the executor arguments after this authority decision.
    approved_a = _authorize(root=tmp_path, intent=intent_a, approval=approval_a)
    assert approved_a

    database = tmp_path / "swap.db"
    task_id = "worker64-swap-task"
    call_id = "worker64-swap-effect"
    guard, ledger, audit = _runtime(database, task_id=task_id)
    handler_calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "external-handler-was-invoked"}

    first_executor = ToolExecutor(audit_log=audit, effect_guard=guard)
    first_executor.register(_spec(), handler)

    # TOCTOU: A was approved above; materially different B is substituted just before execute.
    swapped_call = _call(
        task_id=task_id,
        call_id=call_id,
        arguments=arguments_b,
        approved=approved_a,
    )
    first_result = asyncio.run(first_executor.execute(swapped_call))
    records_before_restart = ledger.list_for_task(task_id)
    events_before_restart = audit.list_for(entity_type="tool_call", entity_id=call_id)

    # Fresh durable state/executor: stale authority for A must still not make B authorized.
    restarted_guard, restarted_ledger, restarted_audit = _runtime(database, task_id=task_id)
    restarted_executor = ToolExecutor(
        audit_log=restarted_audit,
        effect_guard=restarted_guard,
    )
    restarted_executor.register(_spec(), handler)
    restarted_result = asyncio.run(restarted_executor.execute(swapped_call))
    records_after_restart = restarted_ledger.list_for_task(task_id)
    all_events = restarted_audit.list_for(entity_type="tool_call", entity_id=call_id)

    audit_payload_text = json.dumps(
        [event.payload for event in all_events],
        ensure_ascii=False,
        sort_keys=True,
    )
    authority_denial = any(
        event.event_type == "tool.denied"
        and isinstance(event.payload.get("reason"), str)
        and any(
            marker in str(event.payload["reason"]).lower()
            for marker in ("approval", "authority", "mismatch")
        )
        for event in all_events
    )
    no_execution_audit = all(
        event.event_type not in {"tool.started", "tool.completed", "tool.replayed"}
        for event in all_events
    )

    observed = {
        "first_rejected": not first_result.ok,
        "handler_calls_zero": handler_calls == 0,
        "ledger_not_completed_before_restart": all(
            record.status is not IdempotencyStatus.COMPLETED
            for record in records_before_restart
        ),
        "authority_failure_audited": authority_denial,
        "audit_has_no_raw_B_canary": SWAP_CANARY_B not in audit_payload_text,
        "no_started_completed_or_replayed_audit": no_execution_audit,
        "restart_rejected_B": not restarted_result.ok,
        "ledger_not_completed_after_restart": all(
            record.status is not IdempotencyStatus.COMPLETED
            for record in records_after_restart
        ),
        "restart_did_not_add_handler_call": handler_calls == 0,
        "pre_restart_event_count_nonzero": bool(events_before_restart),
    }
    expected = {
        "first_rejected": True,
        "handler_calls_zero": True,
        "ledger_not_completed_before_restart": True,
        "authority_failure_audited": True,
        "audit_has_no_raw_B_canary": True,
        "no_started_completed_or_replayed_audit": True,
        "restart_rejected_B": True,
        "ledger_not_completed_after_restart": True,
        "restart_did_not_add_handler_call": True,
        "pre_restart_event_count_nonzero": True,
    }
    assert observed == expected
