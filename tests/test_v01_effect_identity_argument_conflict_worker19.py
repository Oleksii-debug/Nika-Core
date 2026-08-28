from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.idempotency import IdempotencyLedger
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


def _guard(path: Path, task_id: str) -> tuple[ToolEffectGuard, IdempotencyLedger, SQLiteStore]:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, 'worker19-proof', 'worker19', 'created', '{}', ?, ?)
            """,
            (task_id, "2026-08-28T00:00:00+00:00", "2026-08-28T00:00:00+00:00"),
        )
    ledger = IdempotencyLedger(store)
    return ToolEffectGuard(ledger), ledger, store


def _spec() -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="worker19 durable effect identity proof",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        timeout_seconds=30.0,
    )


def _call(*, task_id: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        call_id="stable-effect",
        tool_id="publish",
        task_id=task_id,
        arguments=arguments,
        approved=True,
    )


def _canonical_approval(*, root: Path, target: str, approval_id: str) -> bool:
    intent = ActionIntent(
        action_id="publish-message",
        tool_id="publish",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target=target,
        approval_required=True,
    )
    approved_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    evidence = ApprovalEvidence(
        approval_id=approval_id,
        action_fingerprint=intent.approval_fingerprint,
        approved_at=approved_at,
        expires_at=approved_at + timedelta(hours=1),
    )
    decision = authorize_action(
        intent,
        SecurityPolicy(
            granted_tools=frozenset({"publish"}),
            sandbox=SandboxPolicy(workspace_root=root),
            budget=ExecutionBudget(),
        ),
        ExecutionBudgetLedger(ExecutionBudget()),
        ApprovalLedger(),
        approval=evidence,
        now=approved_at + timedelta(minutes=1),
    )
    return decision.approved


def test_reordered_nested_mappings_replay_same_canonical_result_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    task_id = "task-reordered"
    guard, _ledger, _store = _guard(database, task_id)
    calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"canonical": "X"}

    original_arguments = {
        "target": {
            "resource": "alpha",
            "metadata": {"first": 1, "second": 2},
        },
        "payload": {"left": "L", "right": "R"},
    }
    reordered_arguments = {
        "payload": {"right": "R", "left": "L"},
        "target": {
            "metadata": {"second": 2, "first": 1},
            "resource": "alpha",
        },
    }

    first = ToolExecutor(effect_guard=guard)
    first.register(_spec(), handler)
    completed = asyncio.run(first.execute(_call(task_id=task_id, arguments=original_arguments)))
    assert completed.ok
    assert completed.output == {"canonical": "X"}
    assert calls == 1

    restarted_guard, _restarted_ledger, _restarted_store = _guard(database, task_id)
    restarted = ToolExecutor(effect_guard=restarted_guard)
    restarted.register(_spec(), handler)
    replayed = asyncio.run(
        restarted.execute(_call(task_id=task_id, arguments=reordered_arguments))
    )

    assert replayed.ok
    assert replayed.output == {"canonical": "X"}
    assert calls == 1


def test_semantically_changed_target_argument_fails_closed_before_handler_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    task_id = "task-target-argument"
    guard, _ledger, _store = _guard(database, task_id)
    calls = 0

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"target": arguments["target"]}

    first = ToolExecutor(effect_guard=guard)
    first.register(_spec(), handler)
    completed = asyncio.run(
        first.execute(
            _call(
                task_id=task_id,
                arguments={"target": "account:A", "payload": "same"},
            )
        )
    )
    assert completed.ok
    assert calls == 1

    restarted_guard, _restarted_ledger, _restarted_store = _guard(database, task_id)
    restarted = ToolExecutor(effect_guard=restarted_guard)
    restarted.register(_spec(), handler)
    conflict = asyncio.run(
        restarted.execute(
            _call(
                task_id=task_id,
                arguments={"target": "account:B", "payload": "same"},
            )
        )
    )

    assert not conflict.ok
    assert calls == 1


def test_changed_canonical_approval_target_cannot_receive_old_effect_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    task_id = "task-approval-binding"
    guard, _ledger, _store = _guard(database, task_id)
    calls = 0

    assert _canonical_approval(root=tmp_path, target="account:A", approval_id="approval-A")

    async def target_a_handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"target": "account:A", "status": "published"}

    stable_arguments = {"payload": "same payload"}
    first = ToolExecutor(effect_guard=guard)
    first.register(_spec(), target_a_handler)
    completed = asyncio.run(first.execute(_call(task_id=task_id, arguments=stable_arguments)))
    assert completed.ok
    assert completed.output == {"target": "account:A", "status": "published"}
    assert calls == 1

    # The canonical security authority now approves a different semantic target while the
    # ToolCall/effect identity and JSON arguments remain unchanged. Durable replay must bind
    # the approved ActionIntent fingerprint (or canonical equivalent), not only a boolean.
    assert _canonical_approval(root=tmp_path, target="account:B", approval_id="approval-B")

    restarted_guard, _restarted_ledger, _restarted_store = _guard(database, task_id)

    async def target_b_handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"target": "account:B", "status": "published"}

    restarted = ToolExecutor(effect_guard=restarted_guard)
    restarted.register(_spec(), target_b_handler)
    replay = asyncio.run(restarted.execute(_call(task_id=task_id, arguments=stable_arguments)))

    assert not replay.ok, "changed canonical approval target received a stale durable result"
    assert calls == 1, "conflicting approval/effect binding reached the handler"


def test_malformed_stored_argument_fingerprint_fails_closed_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    task_id = "task-malformed-digest"
    guard, ledger, store = _guard(database, task_id)
    calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"canonical": "X"}

    call = _call(task_id=task_id, arguments={"target": "account:A", "payload": "same"})
    first = ToolExecutor(effect_guard=guard)
    first.register(_spec(), handler)
    completed = asyncio.run(first.execute(call))
    assert completed.ok
    assert calls == 1

    record = ledger.list_for_task(task_id)[0]
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET input_fingerprint = ? WHERE operation_key = ?",
            ("malformed-not-a-sha256", record.operation_key),
        )

    restarted_guard, _restarted_ledger, _restarted_store = _guard(database, task_id)
    restarted = ToolExecutor(effect_guard=restarted_guard)
    restarted.register(_spec(), handler)
    conflict = asyncio.run(restarted.execute(call))

    assert not conflict.ok
    assert calls == 1
