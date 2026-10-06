from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

import nika_core.product_factory_packaged_build_session as session_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_packaged_build_authority import PackagedBuildAuthorityRuntime
from nika_core.product_factory_packaged_build_session import (
    PackagedBuildRuntimeSessionError,
    build_packaged_build_runtime_session,
)
from nika_core.product_factory_packaged_build_settings import (
    ActivatedPackagedBuildRuntime,
    PackagedBuildRuntimeSettings,
    PackagedBuildRuntimeSettingsError,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import ResourceBudget

PROJECT_ID = "product-" + "a" * 64


def _startup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "python"
    executable.write_text("stub", encoding="utf-8")
    git = tmp_path / "git"
    git.write_text("stub", encoding="utf-8")
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace,
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(str(executable),),
            resource_budget=ResourceBudget(60, 1024 * 1024, 32),
            lease_seconds=120,
        ),
        git_executable=git,
    )


def _config_json() -> str:
    payload = {
        "schema": "nika.product-factory.packaged-build-runtime.v1",
        "node": {
            "node_id": "build-node",
            "platform": "linux",
            "architecture": "x86_64",
            "instance_id": "test",
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
                "platform": "linux",
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


def _activated() -> ActivatedPackagedBuildRuntime:
    node = ExecutionNode(
        NodeIdentity("build-node", Platform.LINUX, "x86_64", "test"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(2, 2048, 4096),
    )
    runtime = object.__new__(PackagedBuildAuthorityRuntime)
    return ActivatedPackagedBuildRuntime(
        node=node,
        runtime=runtime,
        configured_components=frozenset({(PROJECT_ID, "repo", "component")}),
    )


def _configured_settings(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = PackagedBuildRuntimeSettings(store)
    raw = _config_json()
    result = settings.configure({"revision": 0, "config_json": raw})
    assert result.status == "completed"
    return store, settings, raw


def test_unconfigured_session_stays_optional(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = PackagedBuildRuntimeSettings(store)

    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        product_factory_active=True,
    )

    assert session.snapshot()["runtime_status"] == "not_configured"
    assert session.execution_focus() is None
    assert session.post_dispatch_enabled is False


def test_configured_session_requires_active_packaged_pf4(tmp_path):
    store, settings, _raw = _configured_settings(tmp_path)

    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        product_factory_active=False,
    )

    assert session.snapshot()["runtime_status"] == "product_factory_required"
    assert session.execution_focus() == "product-factory-build-authority-json"
    assert session.post_dispatch_enabled is False


def test_session_activates_exact_launch_generation(tmp_path, monkeypatch):
    store, settings, raw = _configured_settings(tmp_path)
    activated = _activated()
    seen = []

    def activate(store_value, *, startup, config):
        seen.append((store_value, startup, config))
        return activated

    monkeypatch.setattr(session_module, "activate_packaged_build_runtime", activate)
    startup = _startup(tmp_path)

    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=startup,
        product_factory_active=True,
    )

    assert session.launch_revision == 1
    assert session.launch_config_json == raw
    assert session.activation is activated
    assert session.snapshot()["runtime_status"] == "active"
    assert session.execution_focus() is None
    assert session.post_dispatch_enabled is True
    assert len(seen) == 1
    assert seen[0][0] is store
    assert seen[0][1] is startup


def test_settings_drift_blocks_post_dispatch_before_pf5_effect(tmp_path, monkeypatch):
    store, settings, raw = _configured_settings(tmp_path)
    monkeypatch.setattr(
        session_module,
        "activate_packaged_build_runtime",
        lambda *_args, **_kwargs: _activated(),
    )
    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        product_factory_active=True,
    )
    assert settings.configure({"revision": 1, "config_json": raw}).status == "completed"

    monkeypatch.setattr(
        session_module,
        "PackagedReviewedBuildContinuation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale PF5 settings must fail before continuation")
        ),
    )

    with pytest.raises(PackagedBuildRuntimeSessionError, match="restart"):
        asyncio.run(session.post_dispatch(cast(Any, object())))

    assert session.snapshot()["runtime_status"] == "restart_required"
    assert session.execution_focus() == "product-factory-build-authority-json"


def test_change_during_activation_never_exposes_stale_runtime(tmp_path, monkeypatch):
    store, settings, raw = _configured_settings(tmp_path)

    def activate(*_args, **_kwargs):
        assert settings.configure({"revision": 1, "config_json": raw}).status == "completed"
        return _activated()

    monkeypatch.setattr(session_module, "activate_packaged_build_runtime", activate)

    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        product_factory_active=True,
    )

    assert session.activation is None
    assert session.post_dispatch_enabled is False
    assert session.snapshot()["runtime_status"] == "restart_required"


def test_activation_failure_is_fail_closed_and_user_safe(tmp_path, monkeypatch):
    store, settings, _raw = _configured_settings(tmp_path)

    def fail(*_args, **_kwargs):
        raise PackagedBuildRuntimeSettingsError("private authority detail")

    monkeypatch.setattr(session_module, "activate_packaged_build_runtime", fail)

    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        product_factory_active=True,
    )

    snapshot = session.snapshot()
    assert snapshot["runtime_status"] == "invalid"
    assert "private authority detail" not in json.dumps(snapshot, ensure_ascii=False)
    assert session.execution_focus() == "product-factory-build-authority-json"


def test_stable_session_runs_canonical_continuation(tmp_path, monkeypatch):
    store, settings, _raw = _configured_settings(tmp_path)
    activated = _activated()
    monkeypatch.setattr(
        session_module,
        "activate_packaged_build_runtime",
        lambda *_args, **_kwargs: activated,
    )
    session = build_packaged_build_runtime_session(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        product_factory_active=True,
    )
    calls = []

    class Continuation:
        def __init__(self, store_value, startup_value, activation_value):
            assert store_value is store
            assert startup_value is session.startup
            assert activation_value is activated

        async def __call__(self, prepared):
            calls.append(prepared)

    monkeypatch.setattr(
        session_module,
        "PackagedReviewedBuildContinuation",
        Continuation,
    )
    prepared = cast(Any, object())

    asyncio.run(session.post_dispatch(prepared))

    assert calls == [prepared]
