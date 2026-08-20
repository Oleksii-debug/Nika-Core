from __future__ import annotations

import pytest

from nika_core.interaction import (
    AmbiguousTargetError,
    ApplicationIdentity,
    ControlLocator,
    ControlNode,
    InteractionTarget,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
    resolve_strict,
    validate_snapshot,
)


def _snapshot(*nodes: ControlNode, generation: int = 1, revision: int = 1) -> SemanticSnapshot:
    return SemanticSnapshot(
        target=InteractionTarget(application=ApplicationIdentity("fixture.exe", 42, 100)),
        generation=generation,
        revision=revision,
        controls=tuple(nodes),
    )


def test_resolve_strict_returns_unique_semantic_match() -> None:
    target = ControlNode("save", "button", "Save", attributes=(("label", "Save document"),))
    snapshot = _snapshot(target, ControlNode("cancel", "button", "Cancel"))
    assert resolve_strict(snapshot, ControlLocator(role="button", name="Save")) == target


def test_resolve_strict_rejects_zero_matches() -> None:
    with pytest.raises(TargetNotFoundError):
        resolve_strict(_snapshot(ControlNode("x", "button", "Other")), ControlLocator(name="Save"))


def test_resolve_strict_rejects_duplicate_names() -> None:
    snapshot = _snapshot(ControlNode("a", "button", "Save"), ControlNode("b", "button", "Save"))
    with pytest.raises(AmbiguousTargetError):
        resolve_strict(snapshot, ControlLocator(role="button", name="Save"))


def test_resolve_can_scope_by_ancestor_semantics() -> None:
    left = ControlNode("left-save", "button", "Save", attributes=(("ancestor_node_id", "left"),))
    right = ControlNode("right-save", "button", "Save", attributes=(("ancestor_node_id", "right"),))
    assert resolve_strict(_snapshot(left, right), ControlLocator(name="Save", ancestor_node_id="right")) == right


def test_bounds_are_not_a_locator_dimension() -> None:
    assert "bounds" not in ControlLocator.__dataclass_fields__


def test_validate_snapshot_accepts_exact_identity() -> None:
    snapshot = _snapshot(ControlNode("save", "button", "Save"))
    validate_snapshot(snapshot, snapshot)


@pytest.mark.parametrize("generation,revision", [(2, 1), (1, 2), (2, 2)])
def test_validate_snapshot_rejects_stale_generation_or_revision(generation: int, revision: int) -> None:
    before = _snapshot(ControlNode("save", "button", "Save"))
    after = _snapshot(ControlNode("save", "button", "Save"), generation=generation, revision=revision)
    with pytest.raises(StaleSnapshotError):
        validate_snapshot(before, after)


def test_validate_snapshot_rejects_target_identity_change() -> None:
    before = _snapshot(ControlNode("save", "button", "Save"))
    after = SemanticSnapshot(
        target=InteractionTarget(application=ApplicationIdentity("fixture.exe", 43, 101)),
        generation=1,
        revision=1,
        controls=before.controls,
    )
    with pytest.raises(StaleSnapshotError):
        validate_snapshot(before, after)
