from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.product_factory_local_coding import (
    ContainedLocalCodingPolicy,
    ContainedLocalCodingProgram,
)
from nika_core.product_factory_local_model_program import (
    build_modelgateway_contained_local_coding_program,
)
from nika_core.toolsmith.contracts import ResourceBudget
from nika_core.training_ollama_manifest import (
    OllamaPromotionManifestStore,
    OllamaPromotionManifestStoreError,
)
from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings

if TYPE_CHECKING:
    from nika_core.product_factory_packaged_bound_local_host import (
        PackagedBoundLocalProductFactoryHost,
    )

_SCHEMA = "nika.product-factory.local-startup.v2"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_REPOSITORIES = 32
_MAX_EXECUTABLES = 32
_MAX_IDENTITY_BYTES = 256
_MAX_JSON_INTEGER_DIGITS = 20


class PackagedLocalProductFactoryStartupError(ValueError):
    """Trusted packaged local Product Factory composition is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class PackagedLocalProductFactoryStartup:
    """Trusted process/resource policy for contained-local Product Factory startup.

    Repository filesystem authority is deliberately absent. It belongs exclusively to
    ProductFactoryLocalRepositoryBindings and is resolved against the exact
    ProductProject/RepositoryGraph at execution time. Model authority remains
    V01ModelSettings.
    """

    workspace_parent: Path
    policy: ContainedLocalCodingPolicy
    git_executable: Path

    def __post_init__(self) -> None:
        workspace_parent = _absolute_path(
            self.workspace_parent,
            "workspace_parent",
        )
        git_executable = _absolute_path(
            self.git_executable,
            "git_executable",
        )
        if type(self.policy) is not ContainedLocalCodingPolicy:
            raise PackagedLocalProductFactoryStartupError(
                "contained-local policy carrier is invalid"
            )
        self.policy.__post_init__()
        object.__setattr__(self, "workspace_parent", workspace_parent)
        object.__setattr__(self, "git_executable", git_executable)


@dataclass(frozen=True, slots=True)
class PackagedLocalProductFactoryProgram:
    """Packaged composition whose execution host resolves repositories per project."""

    multi_repository_host: PackagedBoundLocalProductFactoryHost
    model_authority: _PackagedLocalOllamaAuthority


@dataclass(frozen=True, slots=True)
class _PackagedLocalOllamaAuthority:
    revision: int
    selection_sha256: str
    artifact_pin_sha256: str | None
    model: str
    base_url: str
    private_data_allowed: bool
    timeout_seconds: float
    expected_manifest_sha256: str | None


def decode_packaged_local_product_factory_startup(
    raw: str | None,
) -> PackagedLocalProductFactoryStartup | None:
    """Decode one bounded strict process/resource startup authority from AppConfig."""

    if raw is None:
        return None
    if type(raw) is not str:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup configuration must be text"
        )
    if not raw or raw != raw.strip() or "\x00" in raw:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup configuration is not canonical text"
        )
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup configuration must be UTF-8"
        ) from exc
    if len(encoded) > _MAX_CONFIG_BYTES:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup configuration exceeds the size limit"
        )

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
            parse_int=_bounded_json_int,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup configuration is invalid JSON"
        ) from exc
    if type(payload) is not dict:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup configuration must be an object"
        )
    expected = {
        "schema",
        "workspace_parent",
        "allowed_executables",
        "resource_budget",
        "lease_seconds",
        "git_executable",
    }
    if set(payload) != expected or payload.get("schema") != _SCHEMA:
        raise PackagedLocalProductFactoryStartupError(
            "local Product Factory startup schema does not match"
        )

    executables_raw = payload["allowed_executables"]
    if (
        type(executables_raw) is not list
        or not executables_raw
        or len(executables_raw) > _MAX_EXECUTABLES
    ):
        raise PackagedLocalProductFactoryStartupError(
            "allowed_executables must contain 1..32 paths"
        )
    allowed_executables = tuple(
        str(_json_absolute_path(value, "allowed executable"))
        for value in executables_raw
    )

    budget_raw = payload["resource_budget"]
    if type(budget_raw) is not dict or set(budget_raw) != {
        "timeout_seconds",
        "max_output_bytes",
        "max_changed_files",
    }:
        raise PackagedLocalProductFactoryStartupError(
            "resource_budget has an invalid schema"
        )
    try:
        budget = ResourceBudget(
            timeout_seconds=_exact_int(
                budget_raw["timeout_seconds"],
                "resource timeout",
            ),
            max_output_bytes=_exact_int(
                budget_raw["max_output_bytes"],
                "resource output limit",
            ),
            max_changed_files=_exact_int(
                budget_raw["max_changed_files"],
                "resource file limit",
            ),
        )
        policy = ContainedLocalCodingPolicy(
            allowed_executables=allowed_executables,
            resource_budget=budget,
            lease_seconds=_exact_int(
                payload["lease_seconds"],
                "lease_seconds",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise PackagedLocalProductFactoryStartupError(
            "contained-local execution policy is invalid"
        ) from exc

    return PackagedLocalProductFactoryStartup(
        workspace_parent=_json_absolute_path(
            payload["workspace_parent"],
            "workspace_parent",
        ),
        policy=policy,
        git_executable=_json_absolute_path(
            payload["git_executable"],
            "git_executable",
        ),
    )


def build_packaged_local_product_factory_program(
    store: SQLiteStore,
    *,
    settings: V01ModelSettings,
    startup: PackagedLocalProductFactoryStartup,
) -> PackagedLocalProductFactoryProgram:
    """Compose the packaged plan-scoped host without repository path authority.

    Model binding is validated at startup so invalid/missing model state remains a
    fail-closed configuration error. Concrete workers are created later only after the
    exact ProductProject repository graph resolves through durable local bindings.
    """

    _validate_composition_inputs(store, settings, startup)
    model_authority = _resolve_packaged_local_ollama_authority(store, settings)
    from nika_core.product_factory_packaged_bound_local_host import (
        PackagedBoundLocalProductFactoryHost,
    )

    return PackagedLocalProductFactoryProgram(
        multi_repository_host=PackagedBoundLocalProductFactoryHost(
            store,
            settings=settings,
            startup=startup,
            model_authority=model_authority,
        ),
        model_authority=model_authority,
    )


def build_repository_bound_packaged_local_product_factory_program(
    store: SQLiteStore,
    *,
    settings: V01ModelSettings,
    startup: PackagedLocalProductFactoryStartup,
    repositories: Mapping[str, Path],
) -> ContainedLocalCodingProgram:
    """Build one worker from the current canonical model authority."""

    return _build_repository_bound_packaged_local_product_factory_program_with_authority(
        store,
        settings=settings,
        startup=startup,
        repositories=repositories,
        model_authority=_resolve_packaged_local_ollama_authority(store, settings),
    )


def _build_repository_bound_packaged_local_product_factory_program_with_authority(
    store: SQLiteStore,
    *,
    settings: V01ModelSettings,
    startup: PackagedLocalProductFactoryStartup,
    repositories: Mapping[str, Path],
    model_authority: _PackagedLocalOllamaAuthority,
) -> ContainedLocalCodingProgram:
    """Build one delayed worker from launch-frozen internal model authority."""

    _validate_composition_inputs(store, settings, startup)
    copied = _repository_paths(repositories)
    if type(model_authority) is not _PackagedLocalOllamaAuthority:
        raise TypeError("model_authority carrier is invalid")
    binding = model_authority

    gateway = ModelGateway(audit_log=AuditLog(store))
    gateway.register(
        OllamaProvider(
            default_model=binding.model,
            base_url=binding.base_url,
            think=False,
            expected_manifest_sha256=binding.expected_manifest_sha256,
        ),
        default=True,
    )
    return build_modelgateway_contained_local_coding_program(
        store,
        workspace_parent=startup.workspace_parent,
        repositories=copied,
        gateway=gateway,
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        model=binding.model,
        policy=startup.policy,
        model_timeout_seconds=binding.timeout_seconds,
        git_executable=str(startup.git_executable),
    )

def _validate_composition_inputs(
    store: SQLiteStore,
    settings: V01ModelSettings,
    startup: PackagedLocalProductFactoryStartup,
) -> None:
    if type(store) is not SQLiteStore:
        raise TypeError("store must be SQLiteStore")
    if type(settings) is not V01ModelSettings:
        raise TypeError("settings must be V01ModelSettings")
    if type(startup) is not PackagedLocalProductFactoryStartup:
        raise TypeError("startup carrier is invalid")


def _resolve_packaged_local_ollama_authority(
    store: SQLiteStore,
    settings: V01ModelSettings,
) -> _PackagedLocalOllamaAuthority:
    try:
        revision, selection, artifact_pin = settings.current_binding_with_revision()
    except ModelSetupError as exc:
        raise PackagedLocalProductFactoryStartupError(
            "select a local Ollama model before enabling contained-local Product Factory"
        ) from exc
    if (
        selection.route_kind != "ollama"
        or selection.provider_id != "ollama"
        or selection.provider_kind is not ProviderKind.LOCAL
    ):
        raise PackagedLocalProductFactoryStartupError(
            "contained-local Product Factory startup requires "
            "the persisted Ollama LOCAL route"
        )

    model = _required_route_text(selection.model, "model")
    base_url = _required_route_text(selection.base_url, "base_url")
    expected_manifest_sha256: str | None = None
    if artifact_pin is not None:
        try:
            prepared = OllamaPromotionManifestStore(store).resolve(
                decision_sha256=artifact_pin.decision_sha256,
                binding_sha256=artifact_pin.binding_sha256,
                role=artifact_pin.role,
                artifact_sha256=artifact_pin.artifact_sha256,
                descriptor_digest=artifact_pin.descriptor_digest,
                route_model_id=model,
                base_url=base_url,
            )
            expected_manifest_sha256 = prepared.provider_manifest_sha256
        except (
            TypeError,
            ValueError,
            OllamaPromotionManifestStoreError,
        ) as exc:
            raise PackagedLocalProductFactoryStartupError(
                "persisted promoted Ollama artifact provider manifest "
                "could not be verified"
            ) from exc

    return _PackagedLocalOllamaAuthority(
        revision=revision,
        selection_sha256=hashlib.sha256(
            selection.canonical_json().encode("utf-8")
        ).hexdigest(),
        artifact_pin_sha256=(
            artifact_pin.pin_sha256 if artifact_pin is not None else None
        ),
        model=model,
        base_url=base_url,
        private_data_allowed=selection.private_data_allowed,
        timeout_seconds=selection.timeout_seconds,
        expected_manifest_sha256=expected_manifest_sha256,
    )


def _repository_paths(
    repositories: Mapping[str, Path],
) -> Mapping[str, Path]:
    if not isinstance(repositories, Mapping):
        raise PackagedLocalProductFactoryStartupError(
            "repository bindings must be a mapping"
        )
    if not repositories or len(repositories) > _MAX_REPOSITORIES:
        raise PackagedLocalProductFactoryStartupError(
            "repository bindings must contain 1..32 entries"
        )
    copied: dict[str, Path] = {}
    for repository_id, path in repositories.items():
        identity = _identity(repository_id, "repository_id")
        if identity in copied:
            raise PackagedLocalProductFactoryStartupError(
                "repository identities must be unique"
            )
        copied[identity] = _absolute_path(
            path,
            f"repository {identity}",
        )
    return MappingProxyType(copied)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(raw: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {raw}")


def _bounded_json_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds digit limit")
    return int(raw)


def _exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise PackagedLocalProductFactoryStartupError(
            f"{label} must be an exact integer"
        )
    return value


def _identity(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise PackagedLocalProductFactoryStartupError(
            f"{label} must be canonical non-empty text"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PackagedLocalProductFactoryStartupError(
            f"{label} must be UTF-8 text"
        ) from exc
    if len(encoded) > _MAX_IDENTITY_BYTES:
        raise PackagedLocalProductFactoryStartupError(
            f"{label} exceeds the UTF-8 byte limit"
        )
    return value


def _json_absolute_path(value: object, label: str) -> Path:
    if type(value) is not str:
        raise PackagedLocalProductFactoryStartupError(
            f"{label} path must be exact text"
        )
    return _absolute_path(
        Path(_identity(value, f"{label} path")),
        label,
    )


def _absolute_path(value: object, label: str) -> Path:
    if type(value) is not Path:
        try:
            value = Path(value)
        except (TypeError, ValueError) as exc:
            raise PackagedLocalProductFactoryStartupError(
                f"{label} path is invalid"
            ) from exc
    if not value.is_absolute():
        raise PackagedLocalProductFactoryStartupError(
            f"{label} path must be absolute"
        )
    return value


def _required_route_text(value: object, label: str) -> str:
    return _identity(value, f"persisted {label}")
