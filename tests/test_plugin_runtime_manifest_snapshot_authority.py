from __future__ import annotations

import pytest
from pydantic import ValidationError

from nika_core.plugins.sdk import (
    CapabilityDeclaration,
    PluginCompatibilityError,
    PluginManifest,
    PluginRuntime,
)
from nika_core.tools import ToolRisk


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _manifest(
    *,
    version: str = "1.0.0",
    risk: ToolRisk = ToolRisk.READ_ONLY,
) -> PluginManifest:
    return PluginManifest(
        plugin_id="snapshot.plugin",
        name="Snapshot plugin",
        version=version,
        entrypoint_name="snapshot-plugin",
        capabilities=(
            CapabilityDeclaration(
                capability_id="snapshot.read",
                risk=risk,
                description="Read snapshot evidence",
            ),
        ),
    )


def test_register_detaches_manifest_and_nested_capability_authority() -> None:
    manifest = _manifest()
    adapter = _Adapter(manifest)
    runtime = PluginRuntime()
    runtime.register(manifest, lambda: adapter)

    registered = runtime.manifests()[manifest.plugin_id]
    assert registered == manifest
    assert registered is not manifest
    assert registered.capabilities[0] is not manifest.capabilities[0]

    object.__setattr__(manifest, "version", "9.9.9")
    object.__setattr__(manifest.capabilities[0], "risk", ToolRisk.HIGH_IMPACT)

    durable = runtime.manifests()["snapshot.plugin"]
    assert durable.version == "1.0.0"
    assert durable.capabilities[0].risk is ToolRisk.READ_ONLY

    with pytest.raises(PluginCompatibilityError, match="differs"):
        runtime.activate("snapshot.plugin")
    assert adapter.closed is True


def test_manifest_read_surface_returns_detached_authority() -> None:
    manifest = _manifest()
    runtime = PluginRuntime()
    runtime.register(manifest, lambda: _Adapter(manifest))

    exposed = runtime.manifests()["snapshot.plugin"]
    object.__setattr__(exposed, "version", "forged")

    assert runtime.manifests()["snapshot.plugin"].version == "1.0.0"


def test_upgrade_detaches_replacement_manifest_authority() -> None:
    original = _manifest()
    replacement = _manifest(version="2.0.0")
    adapter = _Adapter(replacement)
    runtime = PluginRuntime()
    runtime.register(original, lambda: _Adapter(original))
    runtime.upgrade(
        replacement,
        lambda: adapter,
        expected_version="1.0.0",
    )

    object.__setattr__(replacement, "version", "3.0.0")

    assert runtime.manifests()["snapshot.plugin"].version == "2.0.0"
    with pytest.raises(PluginCompatibilityError, match="differs"):
        runtime.activate("snapshot.plugin")
    assert adapter.closed is True


class _TruthinessForbiddenCatalog:
    def __init__(self) -> None:
        self.validated: list[str] = []

    def __bool__(self) -> bool:
        raise AssertionError("policy catalog truthiness must not execute")

    def validate(self, manifest: PluginManifest) -> None:
        self.validated.append(manifest.plugin_id)


def test_policy_catalog_is_selected_without_truthiness() -> None:
    catalog = _TruthinessForbiddenCatalog()
    runtime = PluginRuntime(policy_catalog=catalog)  # type: ignore[arg-type]
    runtime.register(_manifest(), lambda: _Adapter(_manifest()))

    assert catalog.validated == ["snapshot.plugin"]


@pytest.mark.parametrize("core_api", [True, 1.0, 0, -1, "1"])
def test_runtime_rejects_non_exact_positive_core_api(core_api: object) -> None:
    with pytest.raises(ValueError, match="exact positive integer"):
        PluginRuntime(core_api=core_api)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("plugin_api_min", True),
        ("plugin_api_min", 1.0),
        ("plugin_api_max", True),
        ("plugin_api_max", 1.0),
    ],
)
def test_manifest_rejects_non_exact_api_integer_fields(field: str, value: object) -> None:
    kwargs: dict[str, object] = {
        "plugin_id": "snapshot.plugin",
        "name": "Snapshot plugin",
        "version": "1.0.0",
        "entrypoint_name": "snapshot-plugin",
        field: value,
    }
    with pytest.raises(ValidationError, match="exact integers"):
        PluginManifest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("expected_version", ["", 1, True])
def test_upgrade_rejects_non_exact_expected_version(expected_version: object) -> None:
    runtime = PluginRuntime()
    original = _manifest()
    replacement = _manifest(version="2.0.0")
    runtime.register(original, lambda: _Adapter(original))

    with pytest.raises(ValueError, match="exact non-empty text"):
        runtime.upgrade(
            replacement,
            lambda: _Adapter(replacement),
            expected_version=expected_version,  # type: ignore[arg-type]
        )
