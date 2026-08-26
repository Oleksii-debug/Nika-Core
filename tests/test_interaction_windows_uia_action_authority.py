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
    """Change action-critical semantics after focus while preserving UIA identity."""

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

    def enumerate_controls(
        self,
        hwnd: int,
        view: str,
    ) -> tuple[UIAControlRecord, ...]:
        assert hwnd == 100
        assert view == "control"
        return (self.record,)

    def focused_identity(
        self,
        hwnd: int,
    ) -> tuple[tuple[int, ...], int] | None:
        assert hwnd == 100
        return self.focused

    def focus(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        assert hwnd == 100
        assert runtime_id == (7, 7)
        assert generation == 1
        self.focused = (runtime_id, generation)
        self.record = replace(self.record, focused=True, **self.drift)

    def invoke(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        assert hwnd == 100
        assert runtime_id == (7, 7)
        assert generation == 1
        self.invoked = True

    def set_value(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        value: str,
    ) -> None:
        raise AssertionError("not used")

    def select(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        raise AssertionError("not used")

    def toggle(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        raise AssertionError("not used")

    def expand(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        raise AssertionError("not used")

    def collapse(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        raise AssertionError("not used")


@pytest.mark.parametrize(
    "drift",
    [
        {"enabled": False},
        {"visible": False},
        {"patterns": ()},
        {"name": "Delete account"},
        {"role": "menuitem"},
    ],
)
def test_action_revalidates_live_semantic_authority_after_focus(
    drift: dict[str, object],
) -> None:
    backend = DriftAfterFocusBackend(drift)
    adapter = WindowsUIAInteractionAdapter(
        process_id=77,
        window_title="Nika Fixture",
        backend=backend,
    )

    validated = adapter.observe().controls[0]
    adapter.focus(validated)
    assert adapter.capture_focus() == validated.node_id

    with pytest.raises(
        (StaleSnapshotError, UnsupportedInteractionError),
        match="stale|changed|disabled|hidden|pattern|semantic|authority",
    ):
        adapter.act(validated, InteractionAction.INVOKE, None)

    assert backend.invoked is False
