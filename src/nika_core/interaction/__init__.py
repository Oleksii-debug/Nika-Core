"""Semantic-first computer interaction contracts."""

from .domain import (
    AmbiguousTargetError,
    ApplicationIdentity,
    BrowserContextIdentity,
    ControlLocator,
    ControlNode,
    FallbackReason,
    InteractionAction,
    InteractionEvidence,
    InteractionResult,
    InteractionTarget,
    PermissionBlockedError,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
    UnsupportedInteractionError,
    WindowIdentity,
)
from .resolver import resolve_strict, validate_snapshot

__all__ = [
    "AmbiguousTargetError",
    "ApplicationIdentity",
    "BrowserContextIdentity",
    "ControlLocator",
    "ControlNode",
    "FallbackReason",
    "InteractionAction",
    "InteractionEvidence",
    "InteractionResult",
    "InteractionTarget",
    "PermissionBlockedError",
    "SemanticSnapshot",
    "StaleSnapshotError",
    "TargetNotFoundError",
    "UnsupportedInteractionError",
    "WindowIdentity",
    "resolve_strict",
    "validate_snapshot",
]
