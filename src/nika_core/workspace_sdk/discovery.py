from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass

from nika_core.workspace_sdk.contracts import (
    WorkspaceEntrypointDescriptor,
    WorkspaceManifest,
    WorkspacePlugin,
    WorkspaceValidationCatalog,
)

WORKSPACE_ENTRYPOINT_GROUP = "nika_core.workspaces"
_entry_points = importlib.metadata.entry_points


@dataclass(frozen=True, slots=True)
class LoadedWorkspacePlugin:
    descriptor: WorkspaceEntrypointDescriptor
    manifest: WorkspaceManifest
    plugin: WorkspacePlugin


@dataclass(frozen=True, slots=True)
class WorkspaceLoadFailure:
    descriptor: WorkspaceEntrypointDescriptor
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class WorkspaceDiscoveryReport:
    loaded: tuple[LoadedWorkspacePlugin, ...]
    failures: tuple[WorkspaceLoadFailure, ...]


def _descriptor(entrypoint) -> WorkspaceEntrypointDescriptor:
    distribution = getattr(entrypoint, "dist", None)
    return WorkspaceEntrypointDescriptor(
        name=str(entrypoint.name),
        value=str(entrypoint.value),
        distribution_name=getattr(distribution, "name", None),
        distribution_version=getattr(distribution, "version", None),
    )


def _raw_entrypoints() -> tuple[object, ...]:
    return tuple(_entry_points(group=WORKSPACE_ENTRYPOINT_GROUP))


def discover_workspace_entrypoints() -> tuple[WorkspaceEntrypointDescriptor, ...]:
    """Enumerate installed workspace entry points without loading plugin code."""

    return tuple(sorted((_descriptor(item) for item in _raw_entrypoints()), key=lambda item: item.name))


def load_workspace_plugins(catalog: WorkspaceValidationCatalog) -> WorkspaceDiscoveryReport:
    """Load and validate plugins independently; one invalid package never blocks the rest."""

    loaded: list[LoadedWorkspacePlugin] = []
    failures: list[WorkspaceLoadFailure] = []
    for entrypoint in _raw_entrypoints():
        descriptor = _descriptor(entrypoint)
        try:
            candidate = entrypoint.load()
            if isinstance(candidate, type):
                candidate = candidate()
            elif getattr(candidate, "manifest", None) is None and callable(candidate):
                candidate = candidate()
            manifest = getattr(candidate, "manifest", None)
            if callable(manifest):
                manifest = manifest()
            if not isinstance(manifest, WorkspaceManifest):
                raise TypeError("workspace entry point must expose a WorkspaceManifest")
            catalog.validate(manifest)
            loaded.append(
                LoadedWorkspacePlugin(descriptor=descriptor, manifest=manifest, plugin=candidate)
            )
        except Exception as exc:  # Plugin import/validation is deliberately isolated per entry point.
            failures.append(
                WorkspaceLoadFailure(
                    descriptor=descriptor,
                    error_type=type(exc).__name__,
                    message=str(exc)[:1000],
                )
            )

    duplicates: dict[tuple[str, int], list[LoadedWorkspacePlugin]] = {}
    for item in loaded:
        duplicates.setdefault((item.manifest.workspace_id, item.manifest.version), []).append(item)
    ambiguous = {key for key, items in duplicates.items() if len(items) > 1}
    if ambiguous:
        retained: list[LoadedWorkspacePlugin] = []
        for item in loaded:
            key = (item.manifest.workspace_id, item.manifest.version)
            if key not in ambiguous:
                retained.append(item)
                continue
            failures.append(
                WorkspaceLoadFailure(
                    descriptor=item.descriptor,
                    error_type="DuplicateWorkspaceVersion",
                    message=f"duplicate workspace identity/version: {key[0]}:{key[1]}",
                )
            )
        loaded = retained

    return WorkspaceDiscoveryReport(
        loaded=tuple(sorted(loaded, key=lambda item: (item.manifest.workspace_id, item.manifest.version))),
        failures=tuple(sorted(failures, key=lambda item: item.descriptor.name)),
    )
