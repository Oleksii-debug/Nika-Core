from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nika_core.activation_authority import ActivationAuthorityPort, ActivationSubject
from nika_core.plugins.entrypoints import (
    EntrypointDescriptor,
    EntrypointLoaderPort,
    discover_entrypoints,
    load_entrypoint,
)
from nika_core.tools import ToolRisk

PLUGIN_ENTRYPOINT_GROUP = "nika_core.plugins"
CURRENT_PLUGIN_API = 1
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_.-]+$"


class PluginCompatibilityError(ValueError):
    """Raised when a plugin cannot be activated against the current Nika SDK."""


class CapabilityDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)]
    risk: ToolRisk = ToolRisk.READ_ONLY
    description: Annotated[str, Field(min_length=1, max_length=500)]


class PluginManifest(BaseModel):
    """Versioned plugin declaration independent from a concrete adapter implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    plugin_id: Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    plugin_api_min: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    plugin_api_max: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    entrypoint_name: Annotated[str, Field(min_length=3, max_length=200)]
    capabilities: tuple[CapabilityDeclaration, ...] = ()
    permission_ids: tuple[
        Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)], ...
    ] = ()
    action_ids: tuple[Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)], ...] = ()

    @field_validator("format_version", "plugin_api_min", "plugin_api_max", mode="before")
    @classmethod
    def require_exact_api_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("plugin API version fields must be exact integers")
        return value

    @field_validator("permission_ids", "action_ids")
    @classmethod
    def normalize_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(normalized) != len(value):
            raise ValueError("manifest identifiers must be unique and non-blank")
        if any("." not in item for item in normalized):
            raise ValueError("manifest permission/action IDs must be stable dotted identifiers")
        return normalized

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


@dataclass(frozen=True, slots=True)
class PluginPolicyCatalog:
    permission_ids: frozenset[str] = frozenset()
    action_ids: frozenset[str] = frozenset()

    def validate(self, manifest: PluginManifest) -> None:
        unknown_permissions = sorted(set(manifest.permission_ids) - self.permission_ids)
        if unknown_permissions:
            raise PluginCompatibilityError(
                "unknown plugin permission IDs: " + ", ".join(unknown_permissions)
            )
        unknown_actions = sorted(set(manifest.action_ids) - self.action_ids)
        if unknown_actions:
            raise PluginCompatibilityError(
                "unknown plugin Action Registry IDs: " + ", ".join(unknown_actions)
            )


@runtime_checkable
class PluginAdapter(Protocol):
    manifest: PluginManifest

    def close(self) -> None: ...


PluginFactory = Callable[[], PluginAdapter]


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    """Static entry-point payload: manifest metadata plus a lazy adapter factory."""

    manifest: PluginManifest
    factory: PluginFactory


def _snapshot_manifest(manifest: PluginManifest) -> PluginManifest:
    if type(manifest) is not PluginManifest:
        raise PluginCompatibilityError("plugin manifest must use the canonical contract")
    try:
        payload = PluginManifest.model_dump(manifest, mode="python")
        snapshot = PluginManifest.model_validate(payload, strict=True)
    except (TypeError, ValueError) as exc:
        raise PluginCompatibilityError("plugin manifest is not canonical") from exc
    return snapshot


def _exact_text_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise ValueError(f"{label} must be an exact tuple of strings")
    return value


@dataclass(frozen=True, slots=True)
class PluginLoadFailure:
    descriptor: EntrypointDescriptor
    error_type: str


@dataclass(frozen=True, slots=True)
class PluginDiscoveryReport:
    registrations: tuple[tuple[EntrypointDescriptor, PluginRegistration], ...]
    failures: tuple[PluginLoadFailure, ...]


def discover_plugin_entrypoints() -> tuple[EntrypointDescriptor, ...]:
    """Discover package metadata without leaking importlib.metadata objects."""
    return discover_entrypoints(PLUGIN_ENTRYPOINT_GROUP)


def inspect_plugin_entrypoints(
    descriptors: tuple[EntrypointDescriptor, ...] | None = None,
) -> PluginDiscoveryReport:
    """Load static registrations independently; one broken package cannot hide the others."""
    selected = descriptors if descriptors is not None else discover_plugin_entrypoints()
    registrations: list[tuple[EntrypointDescriptor, PluginRegistration]] = []
    failures: list[PluginLoadFailure] = []
    for descriptor in selected:
        try:
            loaded = load_entrypoint(descriptor)
            if not isinstance(loaded, PluginRegistration):
                raise TypeError("plugin entry point must expose PluginRegistration")
            manifest = _snapshot_manifest(loaded.manifest)
            if manifest.entrypoint_name != descriptor.name:
                raise PluginCompatibilityError(
                    "manifest entrypoint_name does not match package entry point"
                )
            registrations.append(
                (descriptor, PluginRegistration(manifest=manifest, factory=loaded.factory))
            )
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary third-party import failures.
            failures.append(
                PluginLoadFailure(descriptor=descriptor, error_type=type(exc).__name__)
            )

    duplicate_ids = {
        plugin_id
        for plugin_id, count in Counter(
            registration.manifest.plugin_id for _descriptor, registration in registrations
        ).items()
        if count > 1
    }
    if duplicate_ids:
        retained: list[tuple[EntrypointDescriptor, PluginRegistration]] = []
        for descriptor, registration in registrations:
            if registration.manifest.plugin_id in duplicate_ids:
                failures.append(
                    PluginLoadFailure(
                        descriptor=descriptor,
                        error_type="DuplicatePluginIdentity",
                    )
                )
            else:
                retained.append((descriptor, registration))
        registrations = retained

    return PluginDiscoveryReport(
        registrations=tuple(registrations),
        failures=tuple(failures),
    )


class PluginRuntime:
    """Explicit plugin activation boundary with compatibility and permission checks."""

    def __init__(
        self,
        *,
        core_api: int = CURRENT_PLUGIN_API,
        policy_catalog: PluginPolicyCatalog | None = None,
        activation_authority: ActivationAuthorityPort | None = None,
    ) -> None:
        if type(core_api) is not int or core_api <= 0:
            raise ValueError("core_api must be an exact positive integer")
        self._core_api = core_api
        self._policy_catalog = (
            PluginPolicyCatalog() if policy_catalog is None else policy_catalog
        )
        self._activation_authority = activation_authority
        self._registry_lock = Lock()
        self._deactivating: set[str] = set()
        self._factories: dict[str, tuple[PluginManifest, PluginFactory]] = {}
        self._active: dict[str, PluginAdapter] = {}
        self._effective_permissions: dict[str, tuple[str, ...]] = {}

    def register(self, manifest: PluginManifest, factory: PluginFactory) -> None:
        snapshot = _snapshot_manifest(manifest)
        snapshot.assert_compatible(self._core_api)
        self._policy_catalog.validate(snapshot)
        with self._registry_lock:
            if snapshot.plugin_id in self._factories:
                raise ValueError(f"plugin already registered: {snapshot.plugin_id}")
            self._factories[snapshot.plugin_id] = (snapshot, factory)

    def upgrade(
        self,
        manifest: PluginManifest,
        factory: PluginFactory,
        *,
        expected_version: str,
    ) -> None:
        if type(expected_version) is not str or not expected_version:
            raise ValueError("expected_version must be exact non-empty text")
        snapshot = _snapshot_manifest(manifest)
        snapshot.assert_compatible(self._core_api)
        self._policy_catalog.validate(snapshot)
        with self._registry_lock:
            current = self._factories.get(snapshot.plugin_id)
            if current is None:
                raise KeyError(f"unknown plugin: {snapshot.plugin_id}")
            if snapshot.plugin_id in self._deactivating:
                raise RuntimeError("plugin deactivation is still in progress")
            if snapshot.plugin_id in self._active:
                raise RuntimeError("active plugin must be deactivated before upgrade")
            if current[0].version != expected_version:
                raise ValueError(
                    "plugin upgrade expected_version does not match current registration"
                )
            if snapshot.version == expected_version:
                raise ValueError("plugin upgrade must change the manifest version")
            self._factories[snapshot.plugin_id] = (snapshot, factory)

    def register_entrypoint(self, entrypoint: EntrypointLoaderPort) -> PluginManifest:
        """Compatibility port for an explicitly selected lazy registration loader."""
        loaded = entrypoint.load()
        if not isinstance(loaded, PluginRegistration):
            raise TypeError(
                "plugin entry point must expose PluginRegistration so registration cannot execute "
                "adapter construction"
            )
        manifest = _snapshot_manifest(loaded.manifest)
        if manifest.entrypoint_name != entrypoint.name:
            raise PluginCompatibilityError(
                "manifest entrypoint_name does not match package entry point"
            )
        self.register(manifest, loaded.factory)
        return manifest

    def register_discovered(self, descriptor: EntrypointDescriptor) -> PluginManifest:
        if descriptor.group != PLUGIN_ENTRYPOINT_GROUP:
            raise PluginCompatibilityError("descriptor belongs to another entry-point group")
        loaded = load_entrypoint(descriptor)
        if not isinstance(loaded, PluginRegistration):
            raise TypeError("plugin entry point must expose PluginRegistration")
        manifest = _snapshot_manifest(loaded.manifest)
        if manifest.entrypoint_name != descriptor.name:
            raise PluginCompatibilityError(
                "manifest entrypoint_name does not match package entry point"
            )
        self.register(manifest, loaded.factory)
        return manifest

    def manifests(self) -> Mapping[str, PluginManifest]:
        with self._registry_lock:
            return {
                plugin_id: _snapshot_manifest(pair[0])
                for plugin_id, pair in self._factories.items()
            }

    def activate(
        self,
        plugin_id: str,
        *,
        permission_ids: tuple[str, ...] | None = None,
        approval_refs: tuple[str, ...] = (),
    ) -> PluginAdapter:
        if type(plugin_id) is not str or not plugin_id:
            raise ValueError("plugin_id must be exact non-empty text")
        if permission_ids is not None:
            permission_ids = _exact_text_tuple(
                permission_ids,
                label="permission_ids",
            )
        approval_refs = _exact_text_tuple(approval_refs, label="approval_refs")
        with self._registry_lock:
            if plugin_id in self._deactivating:
                raise RuntimeError("plugin deactivation is still in progress")
            try:
                manifest, factory = self._factories[plugin_id]
            except KeyError as exc:
                raise KeyError(f"unknown plugin: {plugin_id}") from exc
        manifest.assert_compatible(self._core_api)
        self._policy_catalog.validate(manifest)

        if permission_ids is None:
            if manifest.permission_ids:
                raise PermissionError("explicit plugin permission selection is required")
            selected_permissions: tuple[str, ...] = ()
        else:
            if len(permission_ids) != len(set(permission_ids)):
                raise ValueError("duplicate plugin activation permission ID")
            selected_permissions = tuple(sorted(permission_ids))
            undeclared = sorted(set(selected_permissions) - set(manifest.permission_ids))
            if undeclared:
                raise PermissionError(
                    "plugin activation requests undeclared permissions: " + ", ".join(undeclared)
                )

        with self._registry_lock:
            if plugin_id in self._deactivating:
                raise RuntimeError("plugin deactivation is still in progress")
            active = self._active.get(plugin_id)
            if active is not None:
                if self._effective_permissions[plugin_id] != selected_permissions:
                    raise PermissionError(
                        "active plugin permission set differs from requested activation"
                    )
                return active

        high_impact_ids = tuple(
            sorted(
                item.capability_id
                for item in manifest.capabilities
                if item.risk is ToolRisk.HIGH_IMPACT
            )
        )
        subject = ActivationSubject.from_payload(
            kind="plugin",
            subject_id=manifest.plugin_id,
            version=manifest.version,
            payload=manifest.model_dump(mode="json"),
            permission_ids=selected_permissions,
            high_impact_ids=high_impact_ids,
        )
        if subject.requires_authority:
            if self._activation_authority is None:
                raise PermissionError("trusted activation authority is required")
            self._activation_authority.verify(subject, approval_refs)

        adapter = factory()
        if not isinstance(adapter, PluginAdapter):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
            raise TypeError(f"plugin adapter does not satisfy PluginAdapter: {plugin_id}")
        if adapter.manifest != manifest:
            adapter.close()
            raise PluginCompatibilityError(
                "runtime plugin manifest differs from registered manifest"
            )

        publication_error: Exception | None = None
        existing_active: PluginAdapter | None = None
        with self._registry_lock:
            if plugin_id in self._deactivating:
                publication_error = RuntimeError("plugin deactivation is still in progress")
            else:
                current = self._factories.get(plugin_id)
                if current != (manifest, factory):
                    publication_error = PluginCompatibilityError(
                        "plugin registration changed during activation; retry activation"
                    )
                else:
                    active = self._active.get(plugin_id)
                    if active is not None:
                        if self._effective_permissions[plugin_id] != selected_permissions:
                            publication_error = PermissionError(
                                "active plugin permission set differs from requested activation"
                            )
                        else:
                            existing_active = active
                    else:
                        self._active[plugin_id] = adapter
                        self._effective_permissions[plugin_id] = selected_permissions
                        return adapter

        # Adapter code is third-party execution and must never run under the registry lock.
        adapter.close()
        if publication_error is not None:
            raise publication_error
        if existing_active is None:
            raise RuntimeError("plugin activation publication ended without a result")
        return existing_active

    def effective_permissions(self, plugin_id: str) -> tuple[str, ...]:
        with self._registry_lock:
            if plugin_id not in self._active:
                raise KeyError(f"plugin is not active: {plugin_id}")
            return self._effective_permissions[plugin_id]

    def deactivate(self, plugin_id: str) -> None:
        with self._registry_lock:
            if plugin_id in self._deactivating:
                raise RuntimeError("plugin deactivation is already in progress")
            adapter = self._active.pop(plugin_id, None)
            self._effective_permissions.pop(plugin_id, None)
            if adapter is None:
                return
            self._deactivating.add(plugin_id)

        # Clear the fail-stop marker only after teardown is proven complete.
        # If close() raises, this runtime keeps the plugin generation blocked.
        adapter.close()
        with self._registry_lock:
            self._deactivating.remove(plugin_id)
