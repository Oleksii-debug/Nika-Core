"""QA_ONLY Worker69 oracle for V01-B06.

This file deliberately does not patch the production authority or effect contracts.
It proves whether durable completed-effect evidence can be mistaken for current
permission after canonical authority changes.

Target/action-intent substitution is already owned by QA PR #582 and is not
duplicated here. This oracle covers the materially distinct project/permission
authority identity carried by the Product Factory trusted plan.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    trusted_plan_fingerprint,
)
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


def _guard(path: Path, task_id: str) -> ToolEffectGuard:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, 'worker69', 'worker69', 'created', '{}', ?, ?)
            """,
            (task_id, "2026-08-28T00:00:00+00:00", "2026-08-28T00:00:00+00:00"),
        )
    return ToolEffectGuard(IdempotencyLedger(store))


def _spec() -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="controlled external publish",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
    )


def _intent() -> ActionIntent:
    return ActionIntent(
        action_id="publish-controlled-resource",
        tool_id="publish",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target="fixture://resource-a",
        approval_required=True,
    )


def _approval(intent: ActionIntent, approval_id: str) -> ApprovalEvidence:
    approved_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    return ApprovalEvidence(
        approval_id=approval_id,
        action_fingerprint=intent.approval_fingerprint,
        approved_at=approved_at,
        expires_at=approved_at + timedelta(hours=1),
    )


def _policy(root: Path, granted_tools: frozenset[str]) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=granted_tools,
        sandbox=SandboxPolicy(workspace_root=root),
        budget=ExecutionBudget(),
    )


def _trusted_authority(*, project_id: str, permissions: frozenset[str]) -> str:
    request = ComponentWorkRequest(
        work_id=f"worker69-{project_id}",
        project_id=project_id,
        component_id="controlled-component",
        repository_id="repo-main",
        goal="Publish the controlled fixture",
        base_sha="a" * 40,
        allowed_paths=("src/controlled",),
        permission_ceiling=permissions,
        acceptance_commands=(("python", "-m", "pytest", "tests/controlled"),),
    )
    return trusted_plan_fingerprint((request,))


def _complete_once(database: Path) -> tuple[ToolCall, int]:
    calls = 0

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"published": arguments["value"]}

    call = ToolCall(
        call_id="historical-call",
        tool_id="publish",
        task_id="task-authority",
        arguments={"value": "fixture-a"},
        approved=True,
    )
    executor = ToolExecutor(effect_guard=_guard(database, "task-authority"))
    executor.register(_spec(), handler)
    result = asyncio.run(executor.execute(call))
    assert result.ok
    assert result.output == {"published": "fixture-a"}
    assert calls == 1
    return call, calls


def test_permission_downgrade_denial_is_not_overridden_by_historical_completion(
    tmp_path: Path,
) -> None:
    """A completed effect is replay evidence, never a substitute for current permission."""
    database = tmp_path / "state.db"
    intent = _intent()
    now = datetime(2026, 8, 28, 12, 30, tzinfo=UTC)

    authority_a = _policy(tmp_path, frozenset({"publish"}))
    decision_a = authorize_action(
        intent,
        authority_a,
        ExecutionBudgetLedger(authority_a.budget),
        ApprovalLedger(),
        approval=_approval(intent, "approval-a"),
        now=now,
    )
    assert decision_a.approved

    call, calls = _complete_once(database)

    authority_b = _policy(tmp_path, frozenset())
    with pytest.raises(PermissionError, match="tool is not granted"):
        authorize_action(
            intent,
            authority_b,
            ExecutionBudgetLedger(authority_b.budget),
            ApprovalLedger(),
            approval=_approval(intent, "approval-b"),
            now=now,
        )

    async def must_not_run(_arguments: dict[str, object]) -> object:
        raise AssertionError("historical replay must not invoke the external handler")

    restarted = ToolExecutor(effect_guard=_guard(database, "task-authority"))
    restarted.register(_spec(), must_not_run)
    replay = asyncio.run(restarted.execute(call))

    assert not replay.ok, (
        "REAL_DEFECT: canonical authority B denied the tool, but caller-controlled "
        "ToolCall.approved plus historical completed-effect evidence still returns success"
    )
    assert calls == 1


@pytest.mark.parametrize(
    ("project_id", "permissions", "attack"),
    (
        pytest.param(
            "project-a",
            frozenset({"publish", "admin_project"}),
            "permission-upgrade",
            id="permission-upgrade",
        ),
        pytest.param(
            "project-a",
            frozenset({"read_source"}),
            "permission-downgrade",
            id="permission-downgrade",
        ),
        pytest.param(
            "project-b",
            frozenset({"publish"}),
            "project-substitution",
            id="project-substitution",
        ),
    ),
)
def test_completed_effect_does_not_cross_trusted_authority_identity(
    tmp_path: Path,
    project_id: str,
    permissions: frozenset[str],
    attack: str,
) -> None:
    """Project/permission drift must be material to durable effect identity."""
    authority_a = _trusted_authority(
        project_id="project-a",
        permissions=frozenset({"publish"}),
    )
    authority_b = _trusted_authority(project_id=project_id, permissions=permissions)
    assert authority_a != authority_b, f"attack framing did not change authority: {attack}"

    database = tmp_path / f"{attack}.db"
    call, calls = _complete_once(database)

    async def must_not_run(_arguments: dict[str, object]) -> object:
        raise AssertionError("changed authority must fail before external execution")

    restarted = ToolExecutor(effect_guard=_guard(database, "task-authority"))
    restarted.register(_spec(), must_not_run)
    replay = asyncio.run(restarted.execute(call))

    assert not replay.ok, (
        f"REAL_DEFECT: {attack} changed canonical trusted authority "
        f"({authority_a[:12]} -> {authority_b[:12]}), but ToolEffectGuard replay "
        "does not bind project_id/permission_ceiling/trusted_plan_fingerprint"
    )
    assert calls == 1


def test_user_or_principal_scope_is_representable_at_effect_authority_boundary() -> None:
    """A user/principal swap must be representable so it can be rejected deterministically."""
    boundary_fields = (
        set(ActionIntent.__dataclass_fields__)
        | set(ApprovalEvidence.__dataclass_fields__)
        | set(ToolCall.__dataclass_fields__)
    )
    identity_tokens = ("user", "principal", "subject", "actor", "authority")
    represented = {
        name
        for name in boundary_fields
        if any(token in name.casefold() for token in identity_tokens)
    }

    assert represented, (
        "REAL_DEFECT: approval/effect boundary has no explicit user/principal/subject "
        "or canonical authority identity, so user substitution cannot be bound or rejected"
    )


def test_exact_historical_effect_identity_replays_without_duplicate_effect(
    tmp_path: Path,
) -> None:
    """Control: exact historical identity may replay, but must not execute twice."""
    database = tmp_path / "exact.db"
    call, calls = _complete_once(database)

    async def must_not_run(_arguments: dict[str, object]) -> object:
        raise AssertionError("exact replay must not invoke the external handler")

    restarted = ToolExecutor(effect_guard=_guard(database, "task-authority"))
    restarted.register(_spec(), must_not_run)
    replay = asyncio.run(restarted.execute(call))

    assert replay.ok
    assert replay.output == {"published": "fixture-a"}
    assert calls == 1
