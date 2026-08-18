from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PluginFactory = Callable[[], Any]


class PluginManifest(BaseModel):
    """Declarative plugin contract. Loading remains an explicit host decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    plugin_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    api_version: Annotated[str, Field(min_length=1, max_length=32)] = "1"
    capabilities: tuple[str, ...] = ()
    entrypoint: Annotated[str, Field(min_length=3, max_length=240)]

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


class PluginRegistry:
    """In-process registry that does not import arbitrary modules from user input."""

    def __init__(self) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self._factories: dict[str, PluginFactory] = {}

    def register(self, manifest: PluginManifest, factory: PluginFactory) -> None:
        if manifest.plugin_id in self._manifests:
            raise ValueError(f"plugin already registered: {manifest.plugin_id}")
        self._manifests[manifest.plugin_id] = manifest
        self._factories[manifest.plugin_id] = factory

    def manifests(self) -> Mapping[str, PluginManifest]:
        return dict(self._manifests)

    def create(self, plugin_id: str) -> Any:
        try:
            factory = self._factories[plugin_id]
        except KeyError as exc:
            raise KeyError(f"unknown plugin: {plugin_id}") from exc
        return factory()
