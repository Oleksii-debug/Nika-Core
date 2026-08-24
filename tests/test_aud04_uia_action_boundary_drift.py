from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.interaction.domain import (
    InteractionAction,
    StaleSnapshotError,
    UnsupportedInteractionError,
)
from nika_core.interaction.windows_uia_adapter import (
    UIAControlRecord,
    UIAWindowRecord,
    WindowsUIAInteractionAdapter,
)


class DriftAfterFocusBackend:
    """Deterministic backend that changes semantic authority after verified focus."""

    def __init__(self, drift: dict[str, object]) -> None:
        self.drift = drift
        self.started = 123456789
        self.record = UIAControlRecord(
            runtime_id=(7, 7),
            automation_id="save",
            role="button",
            name="Save",
            enabled=True,
            visible=True,
            focused=False,
            value=None,
            bounds=(10, 20, 100, 40),
            class_name="Button",
            framework_id="WinForm",
            patterns=("Invoke",),
            element_generation=1,
        )
        self.focused: tuple[tuple[int, ...], int] | None = None
        self.invoked = False

    def process_started_ns(self, pid: int) -> int:
        assert pid == 77
        return self.started

    def executable(self, pid: int) -> str:
        assert pid == 77
        return r"C:\Program Files\Nika Fixture\fixture.exe"

    def enumerate_windows(self, pid: int) -> tuple[UIAWindowRecord, ...]:
        assert pid == 77
        return (UIAWindowRecord(100, 77, "Nika Fixture"),)

    def enumerate_controls(self, hwnd: int, view: str) -> tuple[UIAControlRecord, ...]:
        assert hwnd == 100
        assert view == "control"
        return (self.record,)

    def focused_identity(self, hwnd: int) -> tuple[tuple[int, ...], int] | None:
        assert hwnd == 100
        return self.focused

    def focus(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        assert hwnd == 100
        assert runtime_id == (7, 7)
        assert generation == 1
        self.focused = (runtime_id, generation)
        # The same UIA identity remains live, but action-critical semantic state changes
        # after focus verification and before act(). This models a real TOCTOU boundary.
        self.record = replace(self.record, focused=True, **self.drift)

    def invoke(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        assert hwnd == 100
        assert runtime_id == (7, 7)
        assert generation == 1
        self.invoked = True

    def set_value(self, hwnd: int, runtime_id: tuple[int, ...], generation: int, value: str) -> None:
        raise AssertionError("not used by this oracle")

    def select(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        raise AssertionError("not used by this oracle")

    def toggle(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        raise AssertionError("not used by this oracle")

    def expand(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        raise AssertionError("not used by this oracle")

    def collapse(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        raise AssertionError("not used by this oracle")


@pytest.mark.parametrize(
    ("label", "drift"),
    [
        ("disabled", {"enabled": False}),
        ("hidden", {"visible": False}),
        ("pattern_removed", {"patterns": ()}),
        ("semantic_name_changed", {"name": "Delete account"}),
    ],
)
def test_uia_action_revalidates_live_semantic_authority_after_focus(
    label: str,
    drift: dict[str, object],
) -> None:
    backend = DriftAfterFocusBackend(drift)
    adapter = WindowsUIAInteractionAdapter(
        process_id=77,
        window_title="Nika Fixture",
        backend=backend,
    )

    validated_node = adapter.observe().controls[0]
    assert validated_node.name == "Save"
    assert validated_node.enabled is True
    assert validated_node.visible is True
    assert "Invoke" in adapter.pattern_capabilities(validated_node)

    adapter.focus(validated_node)
    assert adapter.capture_focus() == validated_node.node_id

    with pytest.raises(
        (StaleSnapshotError, UnsupportedInteractionError),
        match="stale|changed|disabled|hidden|pattern|semantic|authority|snapshot",
    ):
        adapter.act(validated_node, InteractionAction.INVOKE, None)

    assert backend.invoked is False, f"stale semantic authority was used after {label} drift"
