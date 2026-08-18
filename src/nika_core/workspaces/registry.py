from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkspaceSpec(BaseModel):
    """Portable workspace declaration with explicit plugin and data boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    workspace_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    required_plugins: tuple[str, ...] = ()
    data_roots: tuple[str, ...] = ()

    @field_validator("required_plugins", "data_roots")
    @classmethod
    def normalize_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


class WorkspaceResolver:
    """Resolve workspace-relative paths while preventing traversal outside the root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("workspace path escapes configured root")
        return candidate

    def validate_plugins(self, spec: WorkspaceSpec, available: set[str]) -> tuple[str, ...]:
        return tuple(plugin for plugin in spec.required_plugins if plugin not in available)
