from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]*$")


def _normalize_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    invalid = [value for value in normalized if len(value) > 160 or _ID_RE.fullmatch(value) is None]
    if invalid:
        raise ValueError("invalid stable identifier(s): " + ", ".join(invalid))
    return normalized


class WorkspaceManifest(BaseModel):
    """Portable, provider-neutral declaration for an installed Nika workspace plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    workspace_id: Annotated[str, Field(min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    version: Annotated[int, Field(ge=1)] = 1
    sdk_api_version: Annotated[int, Field(ge=1)] = 1
    display_name: Annotated[str, Field(min_length=1, max_length=160)]
    description: Annotated[str, Field(max_length=4000)] = ""
    capability_ids: tuple[str, ...] = ()
    permission_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()

    @field_validator("capability_ids", "permission_ids", "action_ids")
    @classmethod
    def normalize_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_ids(value)

    @model_validator(mode="after")
    def require_dotted_action_ids(self) -> WorkspaceManifest:
        invalid = [action_id for action_id in self.action_ids if "." not in action_id]
        if invalid:
            raise ValueError("action IDs must be stable dotted identifiers: " + ", ".join(invalid))
        return self

    def export_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def import_json(cls, payload: str) -> WorkspaceManifest:
        return cls.model_validate_json(payload)


class WorkspacePlugin(Protocol):
    """Nika-owned plugin contract. Runtime/framework-specific objects stay behind adapters."""

    @property
    def manifest(self) -> WorkspaceManifest: ...


@dataclass(frozen=True, slots=True)
class WorkspaceEntrypointDescriptor:
    name: str
    value: str
    distribution_name: str | None = None
    distribution_version: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceValidationCatalog:
    supported_sdk_api_versions: frozenset[int] = frozenset({1})
    capability_ids: frozenset[str] = frozenset()
    permission_ids: frozenset[str] = frozenset()
    action_ids: frozenset[str] = frozenset()

    def validate(self, manifest: WorkspaceManifest) -> None:
        if manifest.sdk_api_version not in self.supported_sdk_api_versions:
            raise ValueError(f"unsupported workspace SDK API version: {manifest.sdk_api_version}")
        self._reject_unknown("capability", manifest.capability_ids, self.capability_ids)
        self._reject_unknown("permission", manifest.permission_ids, self.permission_ids)
        self._reject_unknown("Action Registry", manifest.action_ids, self.action_ids)

    @staticmethod
    def _reject_unknown(label: str, requested: tuple[str, ...], known: frozenset[str]) -> None:
        unknown = sorted(set(requested) - known)
        if unknown:
            raise ValueError(f"unknown {label} ID(s): " + ", ".join(unknown))


@dataclass(frozen=True, slots=True)
class WorkspaceActivationProposal:
    manifest: WorkspaceManifest
    entrypoint: WorkspaceEntrypointDescriptor
    effective_permission_ids: tuple[str, ...]


def compile_workspace_activation(
    manifest: WorkspaceManifest,
    entrypoint: WorkspaceEntrypointDescriptor,
    *,
    catalog: WorkspaceValidationCatalog,
    approved_permission_ids: frozenset[str],
) -> WorkspaceActivationProposal:
    """Compile a manifest into an activation proposal without widening declared permissions."""

    catalog.validate(manifest)
    missing = sorted(set(manifest.permission_ids) - approved_permission_ids)
    if missing:
        raise PermissionError("workspace permissions require approval: " + ", ".join(missing))
    return WorkspaceActivationProposal(
        manifest=manifest,
        entrypoint=entrypoint,
        effective_permission_ids=tuple(sorted(manifest.permission_ids)),
    )
