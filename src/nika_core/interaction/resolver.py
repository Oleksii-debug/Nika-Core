"""Strict semantic resolution and stale-snapshot validation."""

from __future__ import annotations

from .domain import (
    AmbiguousTargetError,
    ControlLocator,
    ControlNode,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
    node_attributes,
)


def _matches(node: ControlNode, locator: ControlLocator) -> bool:
    if locator.role is not None and node.role != locator.role:
        return False
    if locator.name is not None and node.name != locator.name:
        return False
    attrs = node_attributes(node)
    if locator.label is not None and attrs.get("label") != locator.label:
        return False
    if locator.text is not None and attrs.get("text") != locator.text:
        return False
    if locator.ancestor_node_id is not None and attrs.get("ancestor_node_id") != locator.ancestor_node_id:
        return False
    return all(attrs.get(key) == value for key, value in locator.attributes)


def resolve_strict(snapshot: SemanticSnapshot, locator: ControlLocator) -> ControlNode:
    """Resolve exactly one semantic node; zero and multiple matches fail closed."""
    matches = tuple(node for node in snapshot.controls if _matches(node, locator))
    if not matches:
        raise TargetNotFoundError(f"No semantic target matched {locator!r}")
    if len(matches) != 1:
        raise AmbiguousTargetError(
            f"Semantic target is ambiguous: {len(matches)} controls matched {locator!r}"
        )
    return matches[0]


def validate_snapshot(expected: SemanticSnapshot, current: SemanticSnapshot) -> None:
    """Reject acting on a different target generation or semantic revision."""
    if expected.target != current.target:
        raise StaleSnapshotError("Interaction target identity changed; re-observation is required")
    if expected.generation != current.generation:
        raise StaleSnapshotError("Interaction generation changed; re-observation is required")
    if expected.revision != current.revision:
        raise StaleSnapshotError("Semantic revision changed; re-observation is required")
