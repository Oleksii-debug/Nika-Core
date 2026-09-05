from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EntrypointDescriptor:
    """Provider-neutral installed-package entry-point identity."""

    group: str
    name: str
    value: str
    distribution_name: str | None = None
    distribution_version: str | None = None

    def __post_init__(self) -> None:
        if not self.group.strip() or not self.name.strip() or not self.value.strip():
            raise ValueError("entry-point group, name and value must not be empty")

    def load(self) -> object:
        """Explicitly load this exact discovered identity after metadata-only discovery."""
        return load_entrypoint(self)


class EntrypointLoaderPort(Protocol):
    """Compatibility port for an explicitly selected lazy registration loader."""

    name: str

    def load(self) -> object: ...


def _entry_points(group: str) -> tuple[object, ...]:
    return tuple(importlib.metadata.entry_points(group=group))


def _distribution_identity(entrypoint: object) -> tuple[str | None, str | None]:
    distribution = getattr(entrypoint, "dist", None)
    if distribution is None:
        return None, None
    name = getattr(distribution, "name", None)
    version = getattr(distribution, "version", None)
    if not isinstance(name, str):
        metadata = getattr(distribution, "metadata", None)
        getter = getattr(metadata, "get", None)
        name = getter("Name") if callable(getter) else None
    return (
        name if isinstance(name, str) and name.strip() else None,
        version if isinstance(version, str) and version.strip() else None,
    )


def _describe(group: str, entrypoint: object) -> EntrypointDescriptor:
    name = getattr(entrypoint, "name", None)
    value = getattr(entrypoint, "value", None)
    if not isinstance(name, str) or not isinstance(value, str):
        raise TypeError("installed entry point lacks stable name/value metadata")
    distribution_name, distribution_version = _distribution_identity(entrypoint)
    return EntrypointDescriptor(
        group=group,
        name=name,
        value=value,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
    )


def discover_entrypoints(group: str) -> tuple[EntrypointDescriptor, ...]:
    """Return metadata only; discovery never imports selected package code."""
    descriptors = tuple(_describe(group, item) for item in _entry_points(group))
    return tuple(
        sorted(
            descriptors,
            key=lambda item: (
                item.name,
                item.value,
                item.distribution_name or "",
                item.distribution_version or "",
            ),
        )
    )


def load_entrypoint(descriptor: EntrypointDescriptor) -> object:
    """Load exactly one currently installed entry point matching discovered metadata."""
    matches: list[object] = []
    for entrypoint in _entry_points(descriptor.group):
        if _describe(descriptor.group, entrypoint) == descriptor:
            matches.append(entrypoint)
    if not matches:
        raise LookupError(
            "selected entry point is no longer installed with the discovered identity"
        )
    if len(matches) != 1:
        raise LookupError("selected entry-point identity is ambiguous")
    loader = getattr(matches[0], "load", None)
    if not callable(loader):
        raise TypeError("selected entry point cannot be loaded")
    return loader()
