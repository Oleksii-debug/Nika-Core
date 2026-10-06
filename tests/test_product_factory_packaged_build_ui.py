from __future__ import annotations

import json
from pathlib import Path

from nika_core.config import AppConfig
from scripts import nika_windows

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "product-" + "a" * 64


def _config_json() -> str:
    payload = {
        "schema": "nika.product-factory.packaged-build-runtime.v1",
        "node": {
            "node_id": "build-node",
            "platform": "windows",
            "architecture": "x86_64",
            "instance_id": "desktop",
            "features": ["build"],
            "toolchains": ["python"],
            "gpu": False,
            "resources": {"cpu_cores": 2, "memory_mb": 2048, "disk_mb": 4096},
        },
        "templates": [
            {
                "project_id": PROJECT_ID,
                "repository_id": "repo",
                "component_id": "component",
                "node_id": "build-node",
                "platform": "windows",
                "workspace_relpath": "repo",
                "required_features": ["build"],
                "required_toolchains": ["python"],
                "resources": {"cpu_cores": 1, "memory_mb": 1024, "disk_mb": 2048},
                "command_id": "build",
                "argv": ["python", "-m", "build"],
                "output_paths": ["dist"],
                "max_changed_files": 32,
                "lease_seconds": 120,
                "require_gpu": False,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_pf5_build_controls_are_semantic_keyboard_native_and_secret_safe() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")

    assert '<h3 id="product-factory-build-runtime-heading" tabindex="-1">' in html
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
    assert 'data-error-focus-target="product-factory-build-authority-json"' in html
    assert "не обходить review gate" in html
    assert "не вмикає PF6 deployment" in html
    assert "паролі, токени, API-ключі" in html
    assert html.count('aria-live="') == 1


def test_pf5_build_renderer_uses_revision_cas_and_fail_closed_state() -> None:
    javascript = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")

    for fragment in (
        "function validProductFactoryBuildRuntimeSnapshot(snapshot)",
        "function renderProductFactoryBuildRuntime(snapshot)",
        "productFactoryBuildRuntimeRevision = snapshot.revision",
        "productFactoryBuildRuntimeSave.disabled = true",
        'actionId === "settings.product_factory_build.configure"',
        "payload.revision = productFactoryBuildRuntimeRevision",
        "payload.config_json = raw || null",
        '|| actionId === "settings.product_factory_build.configure"',
        "state.product_factory_build_runtime ?? null",
        "renderProductFactoryBuildRuntime(null)",
        "durable ACCEPTED review state",
    ):
        assert fragment in javascript


def test_windows_bridge_exposes_durable_pf5_settings_with_restart_semantics(
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=(tmp_path / "nika.db").resolve())
    bridge, _products = nika_windows.build_windows_bridge(config)

    initial = bridge.get_state()["state"]["product_factory_build_runtime"]
    assert initial == {
        "status": "ready",
        "revision": 0,
        "configured": False,
        "config_json": None,
        "runtime_status": "not_configured",
    }

    saved = bridge.dispatch(
        {
            "request_id": "pf5-save",
            "action_id": "settings.product_factory_build.configure",
            "payload": {"revision": 0, "config_json": _config_json()},
        }
    )
    assert saved["status"] == "completed"
    assert saved["focus_id"] == "product-factory-build-authority-json"

    current = bridge.get_state()["state"]["product_factory_build_runtime"]
    assert current["status"] == "ready"
    assert current["revision"] == 1
    assert current["configured"] is True
    assert current["config_json"] == _config_json()
    assert current["runtime_status"] == "restart_required"

    stale = bridge.dispatch(
        {
            "request_id": "pf5-stale",
            "action_id": "settings.product_factory_build.configure",
            "payload": {"revision": 0, "config_json": None},
        }
    )
    assert stale["status"] == "rejected"
    assert stale["focus_id"] == "product-factory-build-authority-json"

    refreshed = bridge.dispatch(
        {
            "request_id": "pf5-refresh",
            "action_id": "settings.product_factory_build.refresh",
            "payload": {},
        }
    )
    assert refreshed == {
        "request_id": "pf5-refresh",
        "status": "completed",
        "message": "Збережені налаштування PF5 build runtime перечитано.",
        "focus_id": "product-factory-build-authority-json",
    }


def test_windows_bridge_registers_pf5_settings_and_post_dispatch_wiring() -> None:
    windows = (ROOT / "scripts/nika_windows.py").read_text(encoding="utf-8")
    actions = (ROOT / "src/nika_core/kernel/default_actions.py").read_text(
        encoding="utf-8"
    )

    for action_id in (
        "settings.product_factory_build.configure",
        "settings.product_factory_build.refresh",
    ):
        assert f'"{action_id}"' in windows
        assert f'"{action_id}"' in actions
    assert "PackagedBuildRuntimeSettings(store)" in windows
    assert "build_packaged_build_runtime_session(" in windows
    assert 'state["product_factory_build_runtime"]' in windows
    assert "packaged_build_runtime_session.post_dispatch" in windows
    assert "packaged_build_runtime_session.execution_focus()" in windows
