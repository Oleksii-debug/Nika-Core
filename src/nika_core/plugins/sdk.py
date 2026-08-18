from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nika_core.tools import ToolRisk

PLUGIN_ENTRYPOINT_GROUP = "nika_core.plugins"
CURRENT_PLUGIN_API = 1


class PluginCompatibilityError(ValueError):
    """Raised when a plugin cannot be activated against the current Nika SDK."""


class CapabilityDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    risk: ToolRisk = ToolRisk.READ_ONLY
    description: Annotated[str, Field(min_length=1, max_length=500)]


class PluginManifest(BaseModel):
    """Versioned plugin declaration independent from a concrete adapter implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    plugin_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    plugin_api_min: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    plugin_api_max: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    entrypoint_name: Annotated[str, Field(min_length=3, max_length=200)]
    capabilities: tuple[CapabilityDeclaration, ...] = ()

    @field_validator("capabilities")
    @classmethod
    def reject_duplicate_capabilities(
        cls, value: tuple[CapabilityDeclaration, ...]
    ) -> tuple[CapabilityDeclaration, ...]:
        ids = [item.capability_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate capability_id")
        return value

    @model_validator(mode="after")
    def validate_api_range(self) -> PluginManifest:
        if self.plugin_api_min > self.plugin_api_max:
            raise ValueError("plugin_api_min must not exceed plugin_api_max")
        return self

    def capability_map(self) -> Mapping[str, CapabilityDeclaration]:
        return {item.capability_id: item for item in self.capabilities}

    def assert_compatible(self, core_api: int = CURRENT_PLUGIN_API) -> None:
        if not self.plugin_api_min <= core_api <= self.plugin_api_max:
            raise PluginCompatibilityError(
                f"plugin {self.plugin_id} supports API {self.plugin_api_min}-"
                f"{self.plugin_api_max}, core is {core_api}"
            )


@runtime_checkable
class PluginAdapter(Protocol):
    manifest: PluginManifest

    def close(self) -> None: ...


PluginFactory = Callable[[], PluginAdapter]


def discover_plugin_entrypoints() -> tuple[importlib.metadata.EntryPoint, ...]:
    """Discover package entry points without loading arbitrary plugin code."""
    return tuple(importlib.metadata.entry_points(group=PLUGIN_ENTRYPOINT_GROUP))


class PluginRuntime:
    """Explicit plugin activation boundary with compatibility and manifest checks."""

    def __init__(self, *, core_api: int = CURRENT_PLUGIN_API) -> None:
        self._core_api = core_api
        self._factories: dict[str, tuple[PluginManifest, PluginFactory]] = {}
        self._active: dict[str, PluginAdapter] = {}

    def register(self, manifest: PluginManifest, factory: PluginFactory) -> None:
        manifest.assert_compatible(self._core_api)
        if manifest.plugin_id in self._factories:
            raise ValueError(f"plugin already registered: {manifest.plugin_id}")
        self._factories[manifest.plugin_id] = (manifest, factory)

    def register_entrypoint(self, entrypoint: importlib.metadata.EntryPoint) -> PluginManifest:
        """Load only an explicitly selected entry point and register its factory."""
        loaded: Any = entrypoint.load()
        if not callable(loaded):
            raise TypeError(f"plugin entry point is not callable: {entrypoint.name}")
        factory = loaded
        adapter = factory()
        if not isinstance(adapter, PluginAdapter):
            raise TypeError(f"plugin adapter does not satisfy PluginAdapter: {entrypoint.name}")
        manifest = adapter.manifest
        adapter.close()
        if manifest.entrypoint_name != entrypoint.name:
            raise PluginCompatibilityError("manifest entrypoint_name does not match package entry point")
        self.register(manifest, factory)
        return manifest

    def manifests(self) -> Mapping[str, PluginManifest]:
        return {plugin_id: pair[0] for plugin_id, pair in self._factories.items()}

    def activate(self, plugin_id: str) -> PluginAdapter:
        if plugin_id in self._active:
            return self._active[plugin_id]
        try:
            manifest, factory = self._factories[plugin_id]
        except KeyError as exc:
            raise KeyError(f"unknown plugin: {plugin_id}") from exc
        manifest.assert_compatible(self._core_api)
        adapter = factory()
        if adapter.manifest != manifest:
            raise PluginCompatibilityError("runtime plugin manifest differs from registered manifest")
        self._active[plugin_id] = adapter
        return adapter

    def deactivate(self, plugin_id: str) -> None:
        adapter = self._active.pop(plugin_id, None)
        if adapter is not None:
            adapter.close()
