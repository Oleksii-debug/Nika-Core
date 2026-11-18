from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.security import (
    V01_APPROVAL_AUTHORITY_VERSION,
    ActionIntent,
    ApprovalAuthority,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec

NOW = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
SEED = b"worker62-deterministic-seed-for-tests-only-0123456789"


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.events.append(
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": payload or {},
            }
        )
        return len(self.events)


def _intent(**overrides: object) -> ActionIntent:
    values: dict[str, object] = {
        "action_id": "publish-result",
        "tool_id": "browser.publish",
        "risk": ToolRisk.HIGH_IMPACT,
        "target": "Submit report",
        "task_id": "task-62",
        "project_id": "project-nika-core",
        "site": "EXAMPLE.TEST.",
        "resource": "report/42",
        "arguments": {"title": "Звіт", "nested": {"b": 2, "a": 1}},
        "effect_id": "effect-task62-publish-1",
        "authority_version": V01_APPROVAL_AUTHORITY_VERSION,
        "scope": (("account", "demo"), ("site", "example.test")),
        "network_host": "EXAMPLE.TEST.",
    }
    values.update(overrides)
    return ActionIntent(**values)  # type: ignore[arg-type]


def _authority(*, audit: RecordingAudit | None = None) -> ApprovalAuthority:
    return ApprovalAuthority(issuer_id="nika-host-v01", secret=SEED, audit_sink=audit)


def _policy(tmp_path: Path, authority: ApprovalAuthority) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"browser.publish", "browser.publish.other"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            allowed_network_hosts=("example.test", "other.example.test"),
        ),
        budget=ExecutionBudget(max_network_calls=4),
        approval_verifier=authority.verifier(),
    )


def _issue(authority: ApprovalAuthority, intent: ActionIntent) -> ApprovalEvidence:
    request = authority.request(intent, now=NOW)
    return authority.approve(request.request_id, now=NOW + timedelta(seconds=1))


def _authorize(
    tmp_path: Path,
    authority: ApprovalAuthority,
    intent: ActionIntent,
    evidence: ApprovalEvidence,
) -> None:
    authorize_action(
        intent,
        _policy(tmp_path, authority),
        ExecutionBudgetLedger(ExecutionBudget(max_network_calls=4)),
        ApprovalLedger(),
        approval=evidence,
        now=NOW + timedelta(seconds=2),
    )


def test_normalized_arguments_are_canonical_and_immutable() -> None:
    source = {"e\u0301": {"z": 2, "a": 1}, "items": [1, 2]}
    intent = _intent(arguments=source, site="Example.Test.")
    same = _intent(arguments={"é": {"a": 1, "z": 2}, "items": [1, 2]})
    authority = _authority()
    request = authority.request(intent, now=NOW)

    source["e\u0301"]["a"] = 99
    source["items"].append(3)
    evidence = authority.approve(request.request_id, now=NOW + timedelta(seconds=1))

    assert intent.normalized_arguments_json == same.normalized_arguments_json
    assert intent.effect_fingerprint == same.effect_fingerprint
    assert evidence.action_fingerprint == request.approval_fingerprint
    assert intent.site == "example.test"
    with pytest.raises(TypeError):
        intent.arguments["new"] = "mutation"  # type: ignore[index]


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": "task-63"},
        {"project_id": "project-other"},
        {"tool_id": "browser.publish.other"},
        {"action_id": "publish-result-v2"},
        {"target": "Submit different report"},
        {"site": "other.example.test"},
        {"resource": "report/43"},
        {"arguments": {"title": "Changed"}},
        {"effect_id": "effect-task62-publish-2"},
        {"risk": ToolRisk.EXTERNAL_SIDE_EFFECT},
        {"authority_version": "nika-v01-approval-v2"},
        {"scope": (("account", "other"),)},
        {"approval_required": True},
        {"network_host": "other.example.test"},
    ],
)
def test_material_effect_change_rejects_stale_authority(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    authority = _authority()
    original = _intent()
    evidence = _issue(authority, original)
    with pytest.raises(PermissionError):
        _authorize(tmp_path, authority, replace(original, **changes), evidence)


def test_exact_approval_is_one_shot_and_budget_commits_once(tmp_path: Path) -> None:
    authority = _authority()
    intent = _intent()
    evidence = _issue(authority, intent)
    budget = ExecutionBudgetLedger(ExecutionBudget(max_network_calls=4))
    approvals = ApprovalLedger()
    policy = _policy(tmp_path, authority)

    authorize_action(
        intent,
        policy,
        budget,
        approvals,
        approval=evidence,
        now=NOW + timedelta(seconds=2),
    )
    assert budget.network_calls == 1
    with pytest.raises(PermissionError, match="already used"):
        authorize_action(
            intent,
            policy,
            budget,
            approvals,
            approval=evidence,
            now=NOW + timedelta(seconds=3),
        )
    assert budget.network_calls == 1


def test_caller_cannot_mint_or_tamper_with_approval_evidence(tmp_path: Path) -> None:
    authority = _authority()
    intent = _intent()
    forged = ApprovalEvidence(
        approval_id="forged",
        request_id="forged-request",
        issuer_id=authority.issuer_id,
        authority_version=authority.authority_version,
        action_fingerprint=intent.approval_fingerprint,
        effect_fingerprint=intent.effect_fingerprint,
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        signature="0" * 64,
    )
    with pytest.raises(PermissionError, match="not issued"):
        _authorize(tmp_path, authority, intent, forged)

    issued = _issue(authority, intent)
    tampered = replace(issued, expires_at=issued.expires_at + timedelta(hours=1))
    with pytest.raises(PermissionError, match="not issued"):
        _authorize(tmp_path, authority, intent, tampered)


def test_expiry_scope_lifetime_and_required_identity_fail_closed(tmp_path: Path) -> None:
    authority = _authority()
    intent = _intent()
    request = authority.request(intent, now=NOW, ttl=timedelta(minutes=2))
    evidence = authority.approve(request.request_id, now=NOW + timedelta(seconds=1))
    with pytest.raises(PermissionError, match="currently valid"):
        authorize_action(
            intent,
            _policy(tmp_path, authority),
            ExecutionBudgetLedger(ExecutionBudget(max_network_calls=4)),
            ApprovalLedger(),
            approval=evidence,
            now=request.expires_at,
        )
    with pytest.raises(ValueError, match="at most 15 minutes"):
        authority.request(intent, now=NOW, ttl=timedelta(minutes=16))
    for field_name in ("task_id", "project_id", "effect_id"):
        with pytest.raises(ValueError, match=field_name):
            authority.request(_intent(**{field_name: None}), now=NOW)


def test_display_equivalent_text_cannot_approve_changed_hidden_arguments(tmp_path: Path) -> None:
    authority = _authority()
    original = _intent(arguments={"visible": "same", "hidden": "A"})
    evidence = _issue(authority, original)
    changed = replace(original, arguments={"visible": "same", "hidden": "B"})
    assert original.target == changed.target
    assert original.approval_fingerprint != changed.approval_fingerprint
    with pytest.raises(PermissionError):
        _authorize(tmp_path, authority, changed, evidence)


def test_audit_has_exact_safe_identity_and_rejection_reason(tmp_path: Path) -> None:
    audit = RecordingAudit()
    authority = _authority(audit=audit)
    canary = "TOP-SECRET-CANARY-W62"
    original = _intent(arguments={"credential_like": canary, "value": "A"})
    evidence = _issue(authority, original)
    changed = replace(original, arguments={"credential_like": canary, "value": "B"})
    with pytest.raises(PermissionError, match="exact action"):
        _authorize(tmp_path, authority, changed, evidence)

    assert [item["event_type"] for item in audit.events[:2]] == [
        "security.approval_requested",
        "security.approval_granted",
    ]
    rejected = audit.events[-1]
    payload = rejected["payload"]
    assert rejected["event_type"] == "security.approval_rejected"
    assert payload["reason"] == "stale_exact_action"
    assert payload["task_id"] == changed.task_id
    assert payload["project_id"] == changed.project_id
    assert payload["effect_fingerprint"] == changed.effect_fingerprint
    serialized = json.dumps(audit.events, ensure_ascii=False, sort_keys=True)
    assert canary not in serialized
    assert evidence.signature not in serialized
    assert "arguments" not in payload


def test_toolcall_approved_true_cannot_bypass_host_policy() -> None:
    called = False
    policy_calls = 0

    async def deny(_spec: ToolSpec, _call: ToolCall) -> bool:
        nonlocal policy_calls
        policy_calls += 1
        return False

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "done"

    executor = ToolExecutor(approval_policy=deny)
    executor.register(
        ToolSpec(tool_id="danger", description="danger", risk=ToolRisk.HIGH_IMPACT),
        handler,
    )
    result = asyncio.run(
        executor.execute(ToolCall(call_id="call-1", tool_id="danger", arguments={}, approved=True))
    )
    assert result.error == "approval required"
    assert policy_calls == 1
    assert called is False

    executor_without_policy = ToolExecutor()
    executor_without_policy.register(
        ToolSpec(tool_id="danger", description="danger", risk=ToolRisk.HIGH_IMPACT),
        handler,
    )
    result = asyncio.run(
        executor_without_policy.execute(
            ToolCall(call_id="call-2", tool_id="danger", arguments={}, approved=True)
        )
    )
    assert result.error == "approval required"
    assert called is False


def test_durable_replay_requires_current_matching_exact_authority(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "exact-effect.db")
    store.initialize()
    task_id = TaskQueue(store).create(workspace_id="b06", agent_id="worker62").task_id
    authority = _authority()
    intent_a = _intent(task_id=task_id, target="Account A")
    call = ToolCall(
        call_id="stable-approved-effect",
        tool_id=intent_a.tool_id,
        task_id=task_id,
        arguments=json.loads(intent_a.normalized_arguments_json),
        approved=True,
    )
    spec = ToolSpec(
        tool_id=intent_a.tool_id,
        description="publish exact effect",
        risk=ToolRisk.HIGH_IMPACT,
    )
    handler_calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return {"published": "Account A"}

    def executor_for(intent: ActionIntent) -> ToolExecutor:
        evidence = _issue(authority, intent)

        async def approve(_spec: ToolSpec, _call: ToolCall):
            decision = authorize_action(
                intent,
                _policy(tmp_path, authority),
                ExecutionBudgetLedger(ExecutionBudget(max_network_calls=4)),
                ApprovalLedger(),
                approval=evidence,
                now=NOW + timedelta(seconds=2),
            )
            return decision.tool_authorization

        executor = ToolExecutor(
            approval_policy=approve,
            effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
        )
        executor.register(spec, handler)
        return executor

    swapped_call = replace(
        call,
        arguments={"nested": {"a": 1, "b": 2}, "title": "post-approval swap"},
    )
    swapped = asyncio.run(executor_for(intent_a).execute(swapped_call))

    assert not swapped.ok
    assert swapped.error == "approval required"
    assert handler_calls == 0
    assert IdempotencyLedger(store).list_for_task(task_id) == ()

    completed = asyncio.run(executor_for(intent_a).execute(call))

    assert completed.ok
    assert completed.output == {"published": "Account A"}
    assert handler_calls == 1

    replayed = asyncio.run(executor_for(intent_a).execute(call))

    assert replayed.ok
    assert replayed.output == {"published": "Account A"}
    assert handler_calls == 1

    async def revoked(_spec: ToolSpec, _call: ToolCall):
        raise PermissionError("current permission was revoked")

    revoked_executor = ToolExecutor(
        approval_policy=revoked,
        effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
    )
    revoked_executor.register(spec, handler)
    denied_replay = asyncio.run(revoked_executor.execute(call))

    assert not denied_replay.ok
    assert denied_replay.error == "approval required"
    assert handler_calls == 1

    intent_b = replace(intent_a, target="Account B")
    blocked = asyncio.run(executor_for(intent_b).execute(call))

    assert not blocked.ok
    assert blocked.error == "tool effect not safe to execute"
    assert handler_calls == 1
