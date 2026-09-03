"""Framework-neutral semantic computer-interaction domain contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ApplicationIdentity:
    executable: str
    pid: int | None = None
    process_started_ns: int | None = None


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    application: ApplicationIdentity
    native_handle: int | None
    generation: int


@dataclass(frozen=True, slots=True)
class BrowserContextIdentity:
    session_id: str
    context_id: str
    page_id: str
    document_generation: int
    frame_id: str | None = None
    frame_document_generation: int | None = None


@dataclass(frozen=True, slots=True)
class InteractionTarget:
    application: ApplicationIdentity | None = None
    window: WindowIdentity | None = None
    browser: BrowserContextIdentity | None = None


@dataclass(frozen=True, slots=True)
class ControlNode:
    node_id: str
    role: str
    name: str
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    value: str | None = None
    bounds: tuple[int, int, int, int] | None = None
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticSnapshot:
    target: InteractionTarget
    generation: int
    revision: int
    controls: tuple[ControlNode, ...]


@dataclass(frozen=True, slots=True)
class ControlLocator:
    """Ordered semantic constraints. Bounds/position are deliberately absent."""

    role: str | None = None
    name: str | None = None
    label: str | None = None
    text: str | None = None
    ancestor_node_id: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()


class InteractionAction(StrEnum):
    INVOKE = "invoke"
    SET_VALUE = "set_value"
    SELECT = "select"
    TOGGLE = "toggle"
    EXPAND = "expand"
    COLLAPSE = "collapse"
    FOCUS = "focus"


class FallbackReason(StrEnum):
    SEMANTICS_UNAVAILABLE = "semantics_unavailable"
    SEMANTICS_BROKEN = "semantics_broken"
    UNSUPPORTED_PATTERN = "unsupported_pattern"


@dataclass(frozen=True, slots=True)
class InteractionEvidence:
    snapshot_generation: int
    snapshot_revision: int
    matched_node_id: str | None
    focus_before: str | None
    focus_after: str | None
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class InteractionResult:
    succeeded: bool
    action: InteractionAction
    evidence: InteractionEvidence
    fallback_reason: FallbackReason | None = None
    message: str = ""


class InteractionError(RuntimeError):
    """Base typed interaction failure."""


class TargetNotFoundError(InteractionError):
    pass


class AmbiguousTargetError(InteractionError):
    pass


class StaleSnapshotError(InteractionError):
    pass


class PermissionBlockedError(InteractionError):
    """Terminal permission block; callers must never convert this to coordinate fallback."""


class UnsupportedInteractionError(InteractionError):
    pass


def node_attributes(node: ControlNode) -> Mapping[str, str]:
    return dict(node.attributes)
