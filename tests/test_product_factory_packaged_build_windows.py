from __future__ import annotations

import json
import os
import pathlib
import platform
import sys
from collections.abc import Callable

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.product_factory_packaged_build_pass import PackagedReviewedBuildPass
from nika_core.product_factory_packaged_build_settings import PackagedBuildRuntimeSettings
from nika_core.ui.bridge_models import UIResult
from nika_core.v01_model_settings import V01ModelSettings
from scripts import nika_windows

PROJECT_ID = "product-" + "b" * 64


def _configure_ollama(store: SQLiteStore) -> None:
    result = V01ModelSettings(store).configure(
        {
            "schema_version": 1,
            "revision": 0,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert result.status == "completed"


def _startup_json(tmp_path: pathlib.Path) -> str:
    executable = str(pathlib.Path(sys.executable).resolve())
    workspace = tmp_path / "factory workspaces"
    workspace.mkdir()
    return json.dumps(
        {
            "schema": "nika.product-factory.local-startup.v2",
            "workspace_parent": str(workspace.resolve()),
            "allowed_executables": [executable],
            "resource_budget": {
                "timeout_seconds": 30,
                "max_output_bytes": 1024 * 1024,
                "max_changed_files": 20,
            },
            "lease_seconds": 300,
            "git_executable": executable,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_runtime_payload() -> dict[str, object]:
    executable = str(pathlib.Path(sys.executable).resolve())
    platform_name = "windows" if os.name == "nt" else "linux"
    return {
        "schema": "nika.product-factory.packaged-build-runtime.v1",
        "node": {
            "node_id": "windows-packaged-local" if os.name == "nt" else "linux-packaged-local",
            "platform": platform_name,
            "architecture": platform.machine() or "unknown",
            "instance_id": "packaged-windows-bridge-test",
            "features": ["build"],
            "toolchains": ["python"],
            "gpu": False,
            "resources": {
                "cpu_cores": 2,
                "memory_mb": 2048,
                "disk_mb": 4096,
            },
        },
        "templates": [
            {
                "project_id": PROJECT_ID,
                "repository_id": "repo-core",
                "component_id": "core",
                "node_id": (
                    "windows-packaged-local" if os.name == "nt" else "linux-packaged-local"
                ),
                "platform": platform_name,
                "workspace_relpath": ".",
                "required_features": ["build"],
                "required_toolchains": ["python"],
                "resources": {
                    "cpu_cores": 1,
                    "memory_mb": 512,
                    "disk_mb": 1024,
                },
                "command_id": "python-build",
                "argv": [executable, "-m", "build"],
                "output_paths": ["dist"],
                "max_changed_files": 20,
                "lease_seconds": 300,
                "require_gpu": False,
            }
        ],
    }


def _build_runtime_json(*, lease_seconds: int = 300) -> str:
    payload = _build_runtime_payload()
    templates = payload["templates"]
    assert isinstance(templates, list)
    template = templates[0]
    assert isinstance(template, dict)
    template["lease_seconds"] = lease_seconds
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class _CapturedExecutionController:
    captured: dict[str, object] = {}
    starts: list[str] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).captured = dict(kwargs)

    def start(self, project_id: str) -> UIResult:
        type(self).starts.append(project_id)
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="test controller accepted",
            focus_id="tasks-heading",
        )


def _dispatch_command(
    bridge: object,
    *,
    request_id: str,
    command: str,
) -> dict[str, object]:
    dispatch = getattr(bridge, "dispatch")
    result = dispatch(
        {
            "request_id": request_id,
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert isinstance(result, dict)
    return result


def _configured_bridge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, list[Callable[[], None]], pathlib.Path]:
    database = (tmp_path / "ніка-pf5.db").resolve()
    store = SQLiteStore(database)
    store.initialize()
    _configure_ollama(store)
    saved = PackagedBuildRuntimeSettings(store).configure(
        {"revision": 0, "config_json": _build_runtime_json()}
    )
    assert saved.status == "completed"
    store.close()

    _CapturedExecutionController.captured = {}
    _CapturedExecutionController.starts = []
    monkeypatch.setattr(
        nika_windows,
        "PackagedProductFactoryExecutionController",
        _CapturedExecutionController,
    )
    cleanup: list[Callable[[], None]] = []
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(
            database_path=database,
            product_factory_local_startup_json=_startup_json(tmp_path),
        ),
        start_startup_recovery=False,
        register_cleanup=cleanup.append,
    )
    return bridge, cleanup, database


def test_windows_bridge_activates_launch_frozen_pf5_post_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, cleanup, _database = _configured_bridge(tmp_path, monkeypatch)
    try:
        state = bridge.get_state()
        assert state["ok"] is True
        build_state = state["state"]["product_factory_build_runtime"]
        assert build_state["status"] == "ready"
        assert build_state["configured"] is True
        assert build_state["runtime_status"] == "active"

        post_dispatch = _CapturedExecutionController.captured["post_dispatch"]
        assert callable(post_dispatch)
        assert isinstance(getattr(post_dispatch, "__self__", None), PackagedReviewedBuildPass)
    finally:
        for callback in reversed(cleanup):
            callback()


def test_pf5_settings_change_requires_restart_before_next_factory_run(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, cleanup, database = _configured_bridge(tmp_path, monkeypatch)
    try:
        changed = PackagedBuildRuntimeSettings(SQLiteStore(database)).configure(
            {"revision": 1, "config_json": _build_runtime_json(lease_seconds=301)}
        )
        assert changed.status == "completed"

        state = bridge.get_state()
        assert (
            state["state"]["product_factory_build_runtime"]["runtime_status"]
            == "restart_required"
        )

        created = _dispatch_command(
            bridge,
            request_id="create-before-pf5-restart",
            command="Створи застосунок для доступного каталогу",
        )
        assert created["status"] == "completed"

        run = _dispatch_command(
            bridge,
            request_id="run-after-pf5-change",
            command="Run current Product Factory",
        )
        assert run["status"] == "rejected"
        assert run["focus_id"] == "product-factory-build-authority-json"
        assert _CapturedExecutionController.starts == []
    finally:
        for callback in reversed(cleanup):
            callback()


def test_pf5_build_runtime_actions_are_canonical_keyboard_actions() -> None:
    actions = build_default_action_registry()

    save = actions.get("settings.product_factory_build.configure")
    refresh = actions.get("settings.product_factory_build.refresh")

    assert save.category == "Product Factory"
    assert refresh.category == "Product Factory"
    assert save.default_binding is None
    assert refresh.default_binding is None


def test_pf5_build_runtime_html_exposes_semantic_keyboard_controls() -> None:
    html = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "nika_core"
        / "ui"
        / "web"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert '<label for="product-factory-build-authority-json">' in html
    assert 'id="product-factory-build-authority-json"' in html
    assert 'maxlength="131072"' in html
    assert (
        'aria-describedby="product-factory-build-runtime-help '
        'product-factory-build-runtime-status"'
        in html
    )
    assert 'data-action-id="settings.product_factory_build.configure"' in html
    assert 'data-action-id="settings.product_factory_build.refresh"' in html
    assert html.count(
        'data-error-focus-target="product-factory-build-authority-json"'
    ) >= 2


def test_pf5_build_runtime_js_preserves_revision_dirty_and_fail_closed_state() -> None:
    javascript = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "nika_core"
        / "ui"
        / "web"
        / "app.js"
    ).read_text(encoding="utf-8")

    required_fragments = (
        "let productFactoryBuildRuntimeRevision = 0;",
        "let productFactoryBuildRuntimeDirty = false;",
        "function validProductFactoryBuildRuntimeSnapshot(snapshot)",
        "function renderProductFactoryBuildRuntime(snapshot)",
        "productFactoryBuildRuntimeJson.disabled = true;",
        "snapshot.revision !== productFactoryBuildRuntimeRevision",
        "productFactoryBuildRuntimeDirty = true;",
        "payload.revision = productFactoryBuildRuntimeRevision;",
        "payload.config_json = raw || null;",
        '"settings.product_factory_build.configure"',
        '"settings.product_factory_build.refresh"',
        "renderProductFactoryBuildRuntime(state.product_factory_build_runtime ?? null);",
    )
    for fragment in required_fragments:
        assert fragment in javascript

    configure = javascript.index(
        'if (actionId === "settings.product_factory_build.configure")'
    )
    revision = javascript.index(
        "payload.revision = productFactoryBuildRuntimeRevision;",
        configure,
    )
    config_json = javascript.index("payload.config_json = raw || null;", revision)
    dispatch = javascript.index("globalThis.pywebview.api.dispatch", config_json)
    assert configure < revision < config_json < dispatch
