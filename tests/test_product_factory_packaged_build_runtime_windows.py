from __future__ import annotations

import json
from pathlib import Path

from nika_core.config import AppConfig
from scripts import nika_windows

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "product-" + "a" * 64


def _runtime_json() -> str:
    payload = {
        "schema": "nika.product-factory.packaged-build-runtime.v1",
        "node": {
            "node_id": "local-build",
            "platform": "windows",
            "architecture": "x86_64",
            "instance_id": "desktop",
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
                "node_id": "local-build",
                "platform": "windows",
                "workspace_relpath": ".",
                "required_features": ["build"],
                "required_toolchains": ["python"],
                "resources": {
                    "cpu_cores": 1,
                    "memory_mb": 512,
                    "disk_mb": 1024,
                },
                "command_id": "python-build",
                "argv": ["C:\\Python311\\python.exe", "-m", "build"],
                "output_paths": ["dist"],
                "max_changed_files": 32,
                "lease_seconds": 300,
                "require_gpu": False,
            }
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_windows_bridge_exposes_restart_only_pf5_runtime_settings(
    tmp_path: Path,
) -> None:
    cleanups: list[object] = []
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=(tmp_path / "nika.db").resolve()),
        register_cleanup=cleanups.append,
        start_startup_recovery=False,
    )
    try:
        initial = bridge.get_state()["state"]["product_factory_build_runtime"]
        assert initial["status"] == "ready"
        assert initial["revision"] == 0
        assert initial["configured"] is False
        assert initial["runtime_status"] == "not_configured"

        result = bridge.dispatch(
            {
                "request_id": "configure-pf5-runtime",
                "action_id": "settings.product_factory_build.configure",
                "payload": {"revision": 0, "config_json": _runtime_json()},
            }
        )
        assert result["status"] == "completed"
        assert result["focus_id"] == "product-factory-build-authority-json"

        changed = bridge.get_state()["state"]["product_factory_build_runtime"]
        assert changed["revision"] == 1
        assert changed["configured"] is True
        assert changed["runtime_status"] == "restart_required"
    finally:
        for cleanup in reversed(cleanups):
            if callable(cleanup):
                cleanup()


def test_windows_wiring_uses_canonical_async_pf5_continuation() -> None:
    script = (ROOT / "scripts/nika_windows.py").read_text(encoding="utf-8")
    continuation = (
        ROOT / "src/nika_core/product_factory_packaged_build_continuation.py"
    ).read_text(encoding="utf-8")

    assert "PackagedBuildRuntimeSettings(store)" in script
    assert "activate_packaged_build_runtime(" in script
    assert "PackagedReviewedBuildContinuation(" in script
    assert "post_dispatch=(" in script
    assert "packaged_build_continuation" in script
    assert 'state["product_factory_build_runtime"]' in script
    assert "PackagedReviewedBuildLoopController(" in continuation
    assert "controller.advance_component(" in continuation
    assert "asyncio.to_thread(self._advance, prepared)" in continuation
    assert "deploy_staging(" not in continuation


def test_pf5_runtime_html_uses_semantic_keyboard_controls() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")

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
    assert html.count('aria-live="') == 1


def test_pf5_runtime_js_preserves_revision_dirty_and_uncertain_effect_fence() -> None:
    javascript = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    required = (
        "let productFactoryBuildRuntimeRevision = 0;",
        "let productFactoryBuildRuntimeDirty = false;",
        "function validProductFactoryBuildRuntimeSnapshot(snapshot)",
        "function renderProductFactoryBuildRuntime(snapshot)",
        "productFactoryBuildAuthorityJson.disabled = true;",
        "snapshot.revision !== productFactoryBuildRuntimeRevision",
        "productFactoryBuildRuntimeDirty = true;",
        "payload.revision = productFactoryBuildRuntimeRevision;",
        "payload.config_json = raw || null;",
        '"settings.product_factory_build.configure"',
        '"settings.product_factory_build.refresh"',
        "productFactoryBuildRuntimeDirty = false;",
        'document.documentElement.dataset.nikaReady = "false";',
    )
    for fragment in required:
        assert fragment in javascript
