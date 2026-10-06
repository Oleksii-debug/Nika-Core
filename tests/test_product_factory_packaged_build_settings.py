from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityStore,
    PackagedBuildAuthorityTemplate,
)
from nika_core.product_factory_packaged_build_settings import (
    PackagedBuildRuntimeConfig,
    PackagedBuildRuntimeSettings,
    PackagedBuildRuntimeSettingsError,
    activate_packaged_build_runtime,
    decode_packaged_build_runtime_config,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import ResourceBudget

PROJECT_ID = "product-" + "a" * 64


def _payload(*, node_id: str = "local-build") -> dict[str, object]:
    return {
        "schema": "nika.product-factory.packaged-build-runtime.v1",
        "node": {
            "node_id": node_id,
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
                "node_id": node_id,
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


def _json(payload: dict[str, object] | None = None) -> str:
    return json.dumps(
        _payload() if payload is None else payload,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_decode_packaged_build_runtime_config_binds_node_and_template() -> None:
    decoded = decode_packaged_build_runtime_config(_json())

    assert decoded.node.identity.node_id == "local-build"
    assert decoded.node.identity.platform is Platform.WINDOWS
    assert decoded.node.capabilities.features == frozenset({"build"})
    assert decoded.templates[0].project_id == PROJECT_ID
    assert decoded.templates[0].argv[0] == "C:\\Python311\\python.exe"


def test_decode_rejects_duplicate_json_key() -> None:
    raw = (
        '{"schema":"nika.product-factory.packaged-build-runtime.v1",'
        '"schema":"nika.product-factory.packaged-build-runtime.v1",'
        '"node":{},"templates":[]}'
    )

    with pytest.raises(PackagedBuildRuntimeSettingsError, match="некоректний"):
        decode_packaged_build_runtime_config(raw)


def test_decode_rejects_template_for_another_node() -> None:
    payload = _payload()
    templates = payload["templates"]
    assert isinstance(templates, list)
    template = dict(templates[0])
    template["node_id"] = "other-node"
    payload["templates"] = [template]

    with pytest.raises(PackagedBuildRuntimeSettingsError, match="node або component"):
        decode_packaged_build_runtime_config(_json(payload))


def test_decode_rejects_split_form_credential_argv() -> None:
    payload = _payload()
    templates = payload["templates"]
    assert isinstance(templates, list)
    template = dict(templates[0])
    template["argv"] = ["C:\\Python311\\python.exe", "--token", "secret-value"]
    payload["templates"] = [template]

    with pytest.raises(PackagedBuildRuntimeSettingsError, match="node або component"):
        decode_packaged_build_runtime_config(_json(payload))


def test_settings_use_revision_cas_and_survive_restart(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = PackagedBuildRuntimeSettings(store)

    first = settings.configure({"revision": 0, "config_json": _json()})
    stale = settings.configure({"revision": 0, "config_json": None})

    assert first.status == "completed"
    assert stale.status == "rejected"
    assert settings.saved_json() == _json()
    assert settings.snapshot(runtime_status="restart_required") == {
        "status": "ready",
        "revision": 1,
        "configured": True,
        "config_json": _json(),
        "runtime_status": "restart_required",
    }

    restarted = PackagedBuildRuntimeSettings(SQLiteStore(tmp_path / "nika.db"))
    assert restarted.saved_json() == _json()


def _activation_config(
    tmp_path: Path,
) -> tuple[
    SQLiteStore,
    PackagedLocalProductFactoryStartup,
    PackagedBuildRuntimeConfig,
]:
    store = SQLiteStore(tmp_path / "activation.db")
    store.initialize()
    executable = tmp_path / "python"
    git = tmp_path / "git"
    workspace = tmp_path / "workspaces"
    startup = PackagedLocalProductFactoryStartup(
        workspace_parent=workspace,
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(str(executable),),
            resource_budget=ResourceBudget(
                timeout_seconds=60,
                max_output_bytes=1024 * 1024,
                max_changed_files=64,
            ),
            lease_seconds=600,
        ),
        git_executable=git,
    )
    node = ExecutionNode(
        NodeIdentity(
            node_id="local-build",
            platform=Platform.LINUX,
            architecture="test",
            instance_id="test-instance",
        ),
        NodeCapabilities(
            features=frozenset({"build"}),
            toolchains=frozenset({"python"}),
            gpu=False,
        ),
        ResourceEnvelope(2, 2048, 4096),
    )
    template = PackagedBuildAuthorityTemplate(
        project_id=PROJECT_ID,
        repository_id="repo-core",
        component_id="core",
        node_id="local-build",
        platform=Platform.LINUX,
        workspace_relpath=".",
        required_features=frozenset({"build"}),
        required_toolchains=frozenset({"python"}),
        resources=ResourceEnvelope(1, 512, 1024),
        command_id="python-build",
        argv=(str(executable), "-c", "print('build')"),
        output_paths=("dist",),
        max_changed_files=32,
        lease_seconds=300,
    )
    return store, startup, PackagedBuildRuntimeConfig(node, (template,))


def test_activation_is_restart_idempotent_and_freezes_active_component_set(
    tmp_path: Path,
) -> None:
    store, startup, config = _activation_config(tmp_path)

    first = activate_packaged_build_runtime(store, startup=startup, config=config)
    authorities = PackagedBuildAuthorityStore(
        store,
        node=config.node,
        startup=startup,
    )
    before = authorities.snapshot(
        project_id=PROJECT_ID,
        repository_id="repo-core",
        component_id="core",
    )
    second = activate_packaged_build_runtime(store, startup=startup, config=config)
    after = authorities.snapshot(
        project_id=PROJECT_ID,
        repository_id="repo-core",
        component_id="core",
    )

    assert before.revision == 1
    assert after == before
    assert first.configured_components == frozenset(
        {(PROJECT_ID, "repo-core", "core")}
    )
    assert second.configured_components == first.configured_components


def test_activation_changes_revision_only_when_host_template_changes(
    tmp_path: Path,
) -> None:
    store, startup, config = _activation_config(tmp_path)
    activate_packaged_build_runtime(store, startup=startup, config=config)
    changed_template = replace(
        config.templates[0],
        argv=(config.templates[0].argv[0], "-c", "print('changed')"),
    )
    changed = PackagedBuildRuntimeConfig(config.node, (changed_template,))

    activate_packaged_build_runtime(store, startup=startup, config=changed)
    authority = PackagedBuildAuthorityStore(
        store,
        node=config.node,
        startup=startup,
    ).snapshot(
        project_id=PROJECT_ID,
        repository_id="repo-core",
        component_id="core",
    )

    assert authority.revision == 2
    assert authority.template.argv[-1] == "print('changed')"
