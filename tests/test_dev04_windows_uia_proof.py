from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from nika_core.interaction import (
    ControlLocator,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    SemanticSnapshot,
    StaleSnapshotError,
    resolve_strict,
)
from scripts.dev04_windows_uia_proof import FIXTURE, invoke_and_observe_until


def _action(
    node_id: str = "action-v1",
    *,
    enabled: bool = True,
    visible: bool = True,
    patterns: str = "Invoke",
) -> ControlNode:
    return ControlNode(
        node_id=node_id,
        role="button",
        name="Apply semantic action",
        enabled=enabled,
        visible=visible,
        attributes=(("patterns", patterns),),
    )


def _snapshot(
    revision: int,
    *,
    action_id: str = "action-v1",
    action_enabled: bool = True,
    action_visible: bool = True,
    action_patterns: str = "Invoke",
    status: str | None = None,
) -> SemanticSnapshot:
    controls = [
        _action(
            action_id,
            enabled=action_enabled,
            visible=action_visible,
            patterns=action_patterns,
        )
    ]
    if status is not None:
        controls.append(
            ControlNode(
                node_id=f"status-{revision}",
                role="text",
                name=status,
                enabled=True,
                visible=True,
            )
        )
    return SemanticSnapshot(
        target=InteractionTarget(),
        generation=1,
        revision=revision,
        controls=tuple(controls),
    )


@dataclass
class FakeAdapter:
    snapshots: list[SemanticSnapshot]

    def __post_init__(self) -> None:
        self.act_calls: list[tuple[str, InteractionAction]] = []
        self.observe_calls = 0

    def act(self, node, action: InteractionAction, value) -> None:
        assert value is None
        self.act_calls.append((node.node_id, action))

    def observe(self) -> SemanticSnapshot:
        self.observe_calls += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    @staticmethod
    def pattern_capabilities(node: ControlNode) -> tuple[str, ...]:
        value = dict(node.attributes).get("patterns", "")
        return tuple(part for part in value.split(",") if part)


def _visible_status(snapshot: SemanticSnapshot, name: str) -> bool:
    return resolve_strict(
        snapshot,
        ControlLocator(role="text", name=name),
    ).visible


def test_invoke_waits_for_delayed_semantic_witness_without_replaying_effect() -> None:
    node = _action()
    adapter = FakeAdapter(
        [
            _snapshot(1),
            _snapshot(2),
            _snapshot(3, status="Applied: Доступність перевірено"),
        ]
    )

    result = invoke_and_observe_until(
        adapter,
        node,
        lambda snapshot: _visible_status(
            snapshot,
            "Applied: Доступність перевірено",
        ),
        attempts=3,
        delay_seconds=0,
    )

    assert result.revision == 3
    assert adapter.act_calls == [(node.node_id, InteractionAction.INVOKE)]
    assert adapter.observe_calls == 3


def test_invoke_wait_fails_closed_if_action_identity_changes() -> None:
    node = _action()
    adapter = FakeAdapter([_snapshot(1, action_id="replacement-action")])

    with pytest.raises(StaleSnapshotError, match="identity changed"):
        invoke_and_observe_until(
            adapter,
            node,
            lambda snapshot: False,
            attempts=2,
            delay_seconds=0,
        )

    assert adapter.act_calls == [(node.node_id, InteractionAction.INVOKE)]
    assert adapter.observe_calls == 1


def test_invoke_wait_times_out_without_reissuing_effect() -> None:
    node = _action()
    adapter = FakeAdapter([_snapshot(1), _snapshot(2)])

    with pytest.raises(AssertionError, match="semantic witness"):
        invoke_and_observe_until(
            adapter,
            node,
            lambda snapshot: _visible_status(snapshot, "never appears"),
            attempts=2,
            delay_seconds=0,
        )

    assert adapter.act_calls == [(node.node_id, InteractionAction.INVOKE)]
    assert adapter.observe_calls == 2


def test_invoke_wait_fails_closed_if_actionability_changes() -> None:
    node = _action()
    adapter = FakeAdapter([_snapshot(1, action_enabled=False)])

    with pytest.raises(StaleSnapshotError, match="actionability changed"):
        invoke_and_observe_until(
            adapter,
            node,
            lambda snapshot: False,
            attempts=1,
            delay_seconds=0,
        )

    assert adapter.act_calls == [(node.node_id, InteractionAction.INVOKE)]


def test_invoke_wait_fails_closed_if_invoke_capability_disappears() -> None:
    node = _action()
    adapter = FakeAdapter([_snapshot(1, action_patterns="Value")])

    with pytest.raises(StaleSnapshotError, match="lost Invoke authority"):
        invoke_and_observe_until(
            adapter,
            node,
            lambda snapshot: False,
            attempts=1,
            delay_seconds=0,
        )

    assert adapter.act_calls == [(node.node_id, InteractionAction.INVOKE)]


@pytest.mark.parametrize(
    ("attempts", "delay_seconds", "message"),
    [(0, 0, "attempts must be positive"), (1, -0.1, "delay_seconds")],
)
def test_invoke_wait_rejects_invalid_bounds_before_effect(
    attempts: int,
    delay_seconds: float,
    message: str,
) -> None:
    node = _action()
    adapter = FakeAdapter([_snapshot(1)])

    with pytest.raises(ValueError, match=message):
        invoke_and_observe_until(
            adapter,
            node,
            lambda snapshot: True,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )

    assert adapter.act_calls == []
    assert adapter.observe_calls == 0



def test_fixture_does_not_shadow_powershell_input_automatic_variable() -> None:
    source = FIXTURE.read_text(encoding="utf-8")

    assert re.search(r"\\$input(?![A-Za-z0-9_])", source, flags=re.IGNORECASE) is None
    assert "$problemInput.Text" in source
