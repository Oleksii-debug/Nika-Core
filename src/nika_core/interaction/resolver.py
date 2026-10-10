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
        raise TargetNotFoundError("No semantic target matched the requested constraints")
    if len(matches) != 1:
        raise AmbiguousTargetError(
            f"Semantic target is ambiguous: {len(matches)} controls matched"
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


def validate_action_target(expected: ControlNode, current: ControlNode) -> None:
    """Reject stale or non-actionable semantics before authorizing an adapter effect.

    Focus and screen bounds are observation details, not target identity. A changed
    value, role, name, attributes, visibility or enabled state is action-relevant
    even if an adapter incorrectly reports the same semantic revision/node ID.
    """
    if (
        expected.node_id != current.node_id
        or expected.role != current.role
        or expected.name != current.name
        or expected.value != current.value
        or expected.attributes != current.attributes
    ):
        raise StaleSnapshotError("Semantic action target changed; re-observation is required")
    if (
        expected.enabled is not True
        or current.enabled is not True
        or expected.visible is not True
        or current.visible is not True
    ):
        raise StaleSnapshotError("Semantic action target is disabled or hidden")
