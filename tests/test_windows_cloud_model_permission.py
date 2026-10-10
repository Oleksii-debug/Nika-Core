from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.v01_cloud_model_permission import (
    CloudModelGrantRequest,
    CloudModelPermissionDenied,
)
from nika_core.v01_model_settings import V01ModelSettings
from nika_core.v01_source_settings import V01SourceSettings
from scripts import nika_windows


def _configured_cloud_product(tmp_path: Path) -> AppConfig:
    database = (tmp_path / "Дані Nika" / "ніка.db").resolve()
    config = AppConfig(database_path=database)
    store = SQLiteStore(database)
    store.initialize()

    root = tmp_path / "Джерела команди"
    root.mkdir()
    source_a = root / "перше.txt"
    source_b = root / "друге.txt"
    source_a.write_text("alpha evidence", encoding="utf-8")
    source_b.write_text("beta evidence", encoding="utf-8")
    sources = V01SourceSettings(store, config)
    assert sources.configure(
        {
            "schema_version": 1,
            "root": str(root.resolve()),
            "source_a": str(source_a.resolve()),
            "source_b": str(source_b.resolve()),
            "revision": 0,
        }
    ).status == "completed"

    models = V01ModelSettings(store)
    assert models.configure(
        {
            "schema_version": 1,
            "route_kind": "openai_compatible",
            "provider_id": "configured-api",
            "model": "api-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:PRIVATE_CREDENTIAL_REFERENCE_CANARY",
            "private_data_allowed": True,
            "timeout_seconds": 30,
            "revision": 0,
        }
    ).status == "completed"
    return config


@pytest.mark.parametrize(("native_result", "expected"), [(6, True), (7, False)])
def test_native_cloud_confirmation_is_exact_default_no_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    native_result: int,
    expected: bool,
) -> None:
    calls: list[tuple[object, str, str, int]] = []

    class _User32:
        def MessageBoxW(
            self,
            owner: object,
            message: str,
            title: str,
            flags: int,
        ) -> int:
            calls.append((owner, message, title, flags))
            return native_result

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, use_last_error=True: _User32(),  # type: ignore[attr-defined]
        raising=False,
    )
    request = CloudModelGrantRequest(
        task_id="task-1",
        provider_id="configured-api",
        model="api-model",
        network_host="api.example.test",
        private_data_allowed=True,
    )

    assert nika_windows._confirm_cloud_model_on_windows(request) is expected

    assert len(calls) == 1
    owner, message, title, flags = calls[0]
    assert owner is None
    assert "configured-api" in message
    assert "api-model" in message
    assert "api.example.test" in message
    assert "task-1" not in message
    assert "PRIVATE_CREDENTIAL_REFERENCE_CANARY" not in message
    assert title == "Nika Core — дозвіл зовнішньої моделі"
    assert flags & 0x00000004
    assert flags & 0x00000100
    assert flags & 0x00010000


def test_native_cloud_confirmation_fails_closed_outside_windows() -> None:
    request = CloudModelGrantRequest(
        task_id="task-1",
        provider_id="configured-api",
        model="api-model",
        network_host="api.example.test",
        private_data_allowed=True,
    )
    if sys.platform == "win32":
        pytest.skip("non-Windows fail-closed branch is qualified on non-Windows CI")
    assert nika_windows._confirm_cloud_model_on_windows(request) is False


def test_windows_bridge_denied_cloud_consent_cancels_before_runtime(
    tmp_path: Path,
) -> None:
    config = _configured_cloud_product(tmp_path)
    prompts: list[CloudModelGrantRequest] = []
    bridge, _products = nika_windows.build_windows_bridge(
        config,
        cloud_permission_confirm=lambda request: prompts.append(request) or False,
    )

    result = bridge.dispatch(
        {
            "request_id": "deny-cloud-task",
            "action_id": "task.create",
            "payload": {"command": "Порівняй ці два джерела і дай короткий висновок."},
        }
    )

    assert result["request_id"] == "deny-cloud-task"
    assert result["status"] == "rejected"
    assert result["focus_id"] == "model-route-kind"
    assert "не дозволено" in result["message"]

    store = SQLiteStore(config.database_path)
    queue = TaskQueue(store)
    tasks = queue.list_recent(limit=10)
    assert len(tasks) == 1
    assert tasks[0].state is TaskState.CANCELLED
    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.task_id == tasks[0].task_id
    assert prompt.provider_id == "configured-api"
    assert prompt.model == "api-model"
    assert prompt.network_host == "api.example.test"
    assert "PRIVATE_CREDENTIAL_REFERENCE_CANARY" not in repr(prompt)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0] == 0


def test_desktop_resume_admission_rejection_leaves_task_paused(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "resume.db")
    store.initialize()
    queue = TaskQueue(store)

    def reject_resume(_record: object) -> None:
        raise CloudModelPermissionDenied("resume denied")

    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        admit_resumed_task=reject_resume,
    )
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "resume me"},
    )
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.PAUSED)

    with pytest.raises(CloudModelPermissionDenied, match="resume denied"):
        backend.resume_task({})

    assert queue.get(record.task_id).state is TaskState.PAUSED
    assert backend._active_futures == {}
    assert backend._active_threads == {}

def test_windows_bridge_approved_cloud_consent_grants_before_runtime_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured_cloud_product(tmp_path)
    prompts: list[CloudModelGrantRequest] = []
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        DesktopBackend,
        "_schedule_start",
        lambda _self, task_id, command: scheduled.append((task_id, command)),
    )
    bridge, _products = nika_windows.build_windows_bridge(
        config,
        cloud_permission_confirm=lambda request: prompts.append(request) or True,
    )
    command = "Порівняй джерела через вибрану зовнішню модель."

    result = bridge.dispatch(
        {
            "request_id": "allow-cloud-task",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )

    assert result["status"] == "accepted"
    store = SQLiteStore(config.database_path)
    queue = TaskQueue(store)
    task = queue.list_recent(limit=10)[0]
    assert task.state is TaskState.READY
    assert scheduled == [(task.task_id, command)]
    assert len(prompts) == 1

    with store.connection() as conn:
        binding = conn.execute(
            "SELECT permission_id FROM v01_cloud_model_permission_bindings "
            "WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        assert binding is not None
        permission = conn.execute(
            "SELECT revoked_at FROM standing_permissions WHERE permission_id = ?",
            (binding["permission_id"],),
        ).fetchone()
    assert permission is not None
    assert permission["revoked_at"] is None

