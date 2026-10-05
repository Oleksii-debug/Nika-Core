from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.v01_cloud_model_permission import CloudModelGrantRequest
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
        def MessageBoxW(self, owner: object, message: str, title: str, flags: int) -> int:
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
    assert flags & 0x00000004  # MB_YESNO
    assert flags & 0x00000100  # MB_DEFBUTTON2: default is No
    assert flags & 0x00010000  # MB_SETFOREGROUND


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


def test_windows_session_denied_cloud_consent_cancels_before_runtime(
    tmp_path: Path,
) -> None:
    config = _configured_cloud_product(tmp_path)
    prompts: list[CloudModelGrantRequest] = []
    session = nika_windows.build_windows_session(
        config,
        cloud_permission_confirm=lambda request: prompts.append(request) or False,
    )
    try:
        result = session.bridge.dispatch(
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

        queue = TaskQueue(SQLiteStore(config.database_path))
        tasks = queue.list_recent(limit=10)
        assert len(tasks) == 1
        assert tasks[0].state is TaskState.CANCELLED
        assert session.backend._active_futures == {}
        assert session.backend._active_threads == {}
        assert len(prompts) == 1
        prompt = prompts[0]
        assert prompt.task_id == tasks[0].task_id
        assert prompt.provider_id == "configured-api"
        assert prompt.model == "api-model"
        assert prompt.network_host == "api.example.test"
        assert "PRIVATE_CREDENTIAL_REFERENCE_CANARY" not in repr(prompt)
    finally:
        session.close()
