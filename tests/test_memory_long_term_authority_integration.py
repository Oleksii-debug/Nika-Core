from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService
from nika_core.security import (
    V01_APPROVAL_AUTHORITY_VERSION,
    ActionIntent,
    ApprovalAuthority,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolRisk

NOW = datetime(2026, 9, 10, 17, 25, tzinfo=UTC)
TEST_ONLY_SEED = b"test-only-memory-authority-seed-not-a-secret-0001"


def _memory(tmp_path: Path) -> MemoryService:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return MemoryService(store)


def _intent() -> ActionIntent:
    return ActionIntent(
        action_id="memory.promote",
        tool_id="memory.promote",
        risk=ToolRisk.HIGH_IMPACT,
        target="Promote candidate to durable user memory",
        task_id="task-memory-authority",
        project_id="project-nika-core",
        site=None,
        resource="user/local-user/preferences/language",
        arguments={
            "scope": "user",
            "owner_id": "local-user",
            "namespace": "preferences",
            "key": "language",
        },
        effect_id="effect-memory-promote-language",
        authority_version=V01_APPROVAL_AUTHORITY_VERSION,
        scope=(
            ("memory_scope", "user"),
            ("owner_id", "local-user"),
            ("namespace", "preferences"),
        ),
        network_host=None,
    )


def _authority() -> ApprovalAuthority:
    return ApprovalAuthority(
        issuer_id="nika-test-memory-authority",
        secret=TEST_ONLY_SEED,
    )


def _policy(
    tmp_path: Path,
    authority: ApprovalAuthority,
    *,
    memory_permission: bool,
) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"memory.promote"} if memory_permission else ()),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            allowed_network_hosts=(),
        ),
        budget=ExecutionBudget(max_network_calls=0),
        approval_verifier=authority.verifier(),
    )


def _approved_evidence(authority: ApprovalAuthority, intent: ActionIntent):
    request = authority.request(intent, now=NOW)
    return authority.approve(request.request_id, now=NOW + timedelta(seconds=1))


def _authorize(
    tmp_path: Path,
    authority: ApprovalAuthority,
    intent: ActionIntent,
    approval: object,
    *,
    memory_permission: bool,
) -> None:
    authorize_action(
        intent,
        _policy(tmp_path, authority, memory_permission=memory_permission),
        ExecutionBudgetLedger(ExecutionBudget(max_network_calls=0)),
        ApprovalLedger(),
        approval=approval,
        now=NOW + timedelta(seconds=2),
    )


def _write_user_memory(memory: MemoryService, mutation: str):
    kwargs = {
        "scope": MemoryScope.USER,
        "owner_id": "local-user",
        "namespace": "preferences",
        "key": "language",
        "value": "uk",
        "user_approved": True,
    }
    if mutation == "put":
        return memory.put(**kwargs)
    if mutation == "compare_and_put":
        return memory.compare_and_put(**kwargs, expected_updated_at=None)
    raise AssertionError(f"unexpected mutation: {mutation}")


@pytest.mark.parametrize("mutation", ("put", "compare_and_put"))
def test_ai_candidate_without_authority_remains_non_durable(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Working/candidate data cannot become USER memory without authority."""
    memory = _memory(tmp_path)
    kwargs = {
        "scope": MemoryScope.USER,
        "owner_id": "local-user",
        "namespace": "preferences",
        "key": "language",
        "value": "uk",
        "user_approved": False,
    }

    with pytest.raises(PermissionError):
        if mutation == "put":
            memory.put(**kwargs)
        else:
            memory.compare_and_put(**kwargs, expected_updated_at=None)

    assert memory.get(
        scope=MemoryScope.USER,
        owner_id="local-user",
        namespace="preferences",
        key="language",
    ) is None


@pytest.mark.parametrize("mutation", ("put", "compare_and_put"))
def test_trusted_approved_path_can_promote_candidate(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Positive control: current trusted approval flow can authorize promotion."""
    authority = _authority()
    intent = _intent()
    evidence = _approved_evidence(authority, intent)
    _authorize(
        tmp_path,
        authority,
        intent,
        evidence,
        memory_permission=True,
    )

    record = _write_user_memory(_memory(tmp_path), mutation)

    assert record.scope is MemoryScope.USER
    assert record.user_approved is True
    assert record.value == "uk"


@pytest.mark.parametrize("mutation", ("put", "compare_and_put"))
def test_denied_candidate_cannot_self_assert_user_approved(
    tmp_path: Path,
    mutation: str,
) -> None:
    """A denied approval must dominate any caller-supplied approval boolean."""
    authority = _authority()
    intent = _intent()
    request = authority.request(intent, now=NOW)
    authority.deny(request.request_id, now=NOW + timedelta(seconds=1))
    memory = _memory(tmp_path)

    with pytest.raises(PermissionError):
        _write_user_memory(memory, mutation)

    assert memory.get(
        scope=MemoryScope.USER,
        owner_id="local-user",
        namespace="preferences",
        key="language",
    ) is None


@pytest.mark.parametrize("mutation", ("put", "compare_and_put"))
def test_revoked_permission_cannot_be_bypassed_by_user_approved_boolean(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Permission revoked after approval must fail closed before durable promotion."""
    authority = _authority()
    intent = _intent()
    evidence = _approved_evidence(authority, intent)

    with pytest.raises(PermissionError):
        _authorize(
            tmp_path,
            authority,
            intent,
            evidence,
            memory_permission=False,
        )

    memory = _memory(tmp_path)
    with pytest.raises(PermissionError):
        _write_user_memory(memory, mutation)

    assert memory.get(
        scope=MemoryScope.USER,
        owner_id="local-user",
        namespace="preferences",
        key="language",
    ) is None
