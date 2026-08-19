from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.security import ActionIntent, ApprovalAuthority
from nika_core.tools import ToolRisk
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


def build_backend(
    tmp_path: Path,
    *,
    approval_authority: ApprovalAuthority | None = None,
) -> tuple[DesktopBackend, TaskQueue, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        approval_authority=approval_authority,
    )
    return backend, queue, store


def test_desktop_bootstrap_exposes_real_agent_and_workspace(tmp_path: Path) -> None:
    backend, _queue, _store = build_backend(tmp_path)
    snapshot = backend.snapshot()
    assert snapshot["agents"][0]["name"] == "Nika"
    assert snapshot["workspaces"][0]["name"] == "Основний"
    assert snapshot["tasks"] == []
    assert snapshot["pending_approvals"] == []


def test_create_task_rejects_empty_command(tmp_path: Path) -> None:
    backend, _queue, _store = build_backend(tmp_path)
    with pytest.raises(ValueError, match="Введіть команду"):
        backend.create_task({"command": "   "})


def test_create_task_runs_real_no_llm_runtime_and_persists_result(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    result = backend.create_task({"command": "перевір локальний стан"})
    assert result.status == "completed"
    tasks = queue.list_recent()
    assert len(tasks) == 1
    assert tasks[0].state == TaskState.COMPLETED
    assert tasks[0].payload["command"] == "перевір локальний стан"
    snapshot = backend.snapshot()
    assert snapshot["tasks"][0]["state"] == "COMPLETED"


def test_pause_resume_and_stop_use_persisted_task_state(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(workspace_id="default", agent_id="nika.default", payload={"command": "x"})
    queue.transition(task.task_id, TaskState.READY)
    assert backend.pause_task({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.PAUSED
    assert backend.resume_task({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.READY
    assert backend.stop_agent({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.CANCELLED


def test_stop_fails_closed_for_running_task_without_runtime_session(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(workspace_id="default", agent_id="nika.default", payload={"command": "x"})
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    with pytest.raises(ValueError, match="без збереженої сесії"):
        backend.stop_agent({})

    assert queue.get(task.task_id).state == TaskState.RUNNING


def test_bridge_returns_read_only_desktop_snapshot(tmp_path: Path) -> None:
    backend, _queue, store = build_backend(tmp_path)
    actions = build_default_action_registry()
    bridge = UIActionBridge(actions, Keymap(store, actions), state_provider=backend.snapshot)
    response = bridge.get_state()
    assert response["ok"] is True
    assert response["state"]["agents"][0]["agent_id"] == "nika.default"


def test_pending_r4_snapshot_contains_view_only_not_signed_evidence(tmp_path: Path) -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"d" * 32)
    backend, _queue, _store = build_backend(tmp_path, approval_authority=authority)
    intent = ActionIntent(
        action_id="privacy-delete",
        tool_id="files.delete",
        risk=ToolRisk.HIGH_IMPACT,
        target="named private export",
        write_path="artifacts/private-export.txt",
        approval_required=True,
    )
    request = authority.request(intent, reason="delete reviewed private export")

    view = backend.snapshot()["pending_approvals"][0]
    assert view["request_id"] == request.request_id
    assert view["action_id"] == intent.action_id
    assert view["target"] == intent.target
    assert "signature" not in view
    assert "approval_id" not in view
    assert "issuer_id" not in view


def test_desktop_approve_uses_request_id_only_and_preserves_original_action(tmp_path: Path) -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"d" * 32)
    backend, _queue, _store = build_backend(tmp_path, approval_authority=authority)
    intent = ActionIntent(
        action_id="release-publish",
        tool_id="release.publish",
        risk=ToolRisk.HIGH_IMPACT,
        target="release v1",
        approval_required=True,
    )
    request = authority.request(intent, reason="publish reviewed release")

    result = backend.approve_action(
        {
            "request_id": request.request_id,
            "target": "attacker-supplied different release",
            "tool_id": "attacker.tool",
        }
    )

    assert result.status == "completed"
    evidence = authority.evidence(request.request_id)
    assert evidence.action_fingerprint == intent.approval_fingerprint
    assert backend.snapshot()["pending_approvals"] == []


def test_desktop_denial_removes_pending_request(tmp_path: Path) -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"d" * 32)
    backend, _queue, _store = build_backend(tmp_path, approval_authority=authority)
    intent = ActionIntent(
        action_id="legal-submit",
        tool_id="legal.submit",
        risk=ToolRisk.HIGH_IMPACT,
        target="named filing",
        approval_required=True,
    )
    request = authority.request(intent, reason="submit reviewed filing")

    assert backend.deny_action({"request_id": request.request_id}).status == "completed"
    assert backend.snapshot()["pending_approvals"] == []
    with pytest.raises(PermissionError, match="denied"):
        authority.approve(request.request_id)


def test_desktop_approval_fails_closed_without_trusted_authority(tmp_path: Path) -> None:
    backend, _queue, _store = build_backend(tmp_path)
    with pytest.raises(ValueError, match="канал людського підтвердження недоступний"):
        backend.approve_action({"request_id": "r4-missing"})
    with pytest.raises(ValueError, match="канал людського підтвердження недоступний"):
        backend.deny_action({"request_id": "r4-missing"})
