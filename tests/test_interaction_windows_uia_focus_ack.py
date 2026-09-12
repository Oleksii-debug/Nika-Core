from __future__ import annotations

from dataclasses import replace

import pytest

import nika_core.interaction.windows_uia_adapter as uia_module
from nika_core.interaction.domain import (
    AmbiguousTargetError,
    InteractionAction,
    StaleSnapshotError,
)
from nika_core.interaction.windows_uia_adapter import (
    UIAControlRecord,
    UIAWindowRecord,
    WindowsUIAInteractionAdapter,
)


def _record(*, generation: int = 1, name: str = "Save") -> UIAControlRecord:
    return UIAControlRecord(
        runtime_id=(1, 2),
        automation_id="save",
        role="button",
        name=name,
        enabled=True,
        visible=True,
        focused=False,
        value=None,
        bounds=(10, 20, 100, 40),
        class_name="Button",
        framework_id="WinForm",
        patterns=("Invoke",),
        element_generation=generation,
    )


class FocusAckBackend:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.started = 123456789
        self.windows = [UIAWindowRecord(100, 77, "Nika Fixture")]
        self.controls = [_record()]
        self.focus_calls = 0
        self.focused_reads = 0

    def process_started_ns(self, pid: int) -> int:
        assert pid == 77
        return self.started

    def executable(self, pid: int) -> str:
        assert pid == 77
        return r"C:\Program Files\Nika Fixture\fixture.exe"

    def enumerate_windows(self, pid: int) -> tuple[UIAWindowRecord, ...]:
        assert pid == 77
        return tuple(self.windows)

    def enumerate_controls(self, hwnd: int, view: str) -> tuple[UIAControlRecord, ...]:
        assert hwnd == 100
        assert view in {"control", "content"}
        if self.focus_calls:
            if self.mode == "replacement":
                return (_record(generation=2),)
            if self.mode == "missing":
                return ()
            if self.mode == "ambiguous":
                return (_record(), replace(_record(), name="Duplicate wrapper"))
        return tuple(self.controls)

    def focused_identity(self, hwnd: int) -> tuple[tuple[int, ...], int] | None:
        assert hwnd == 100
        self.focused_reads += 1
        if self.mode == "lag_then_success" and self.focused_reads >= 3:
            return ((1, 2), 1)
        if self.mode == "timeout":
            return ((9, 9), 1)
        return None

    def focus(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        assert hwnd == 100
        assert (runtime_id, generation) == ((1, 2), 1)
        self.focus_calls += 1


def _adapter(backend: FocusAckBackend) -> WindowsUIAInteractionAdapter:
    return WindowsUIAInteractionAdapter(
        process_id=77,
        window_title="Nika Fixture",
        backend=backend,
    )


def _remove_focus_sleep(monkeypatch: pytest.MonkeyPatch, *, attempts: int = 4) -> None:
    monkeypatch.setattr(uia_module, "_FOCUS_ACK_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(uia_module, "_FOCUS_ACK_ATTEMPTS", attempts)


def test_focus_effect_is_issued_once_while_provider_acknowledgement_is_polled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("lag_then_success")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    adapter.focus(node)

    assert backend.focus_calls == 1
    assert backend.focused_reads == 3


def test_focus_action_uses_same_single_effect_acknowledgement_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("lag_then_success")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    adapter.act(node, InteractionAction.FOCUS, None)

    assert backend.focus_calls == 1
    assert backend.focused_reads == 3


def test_focus_acknowledgement_rejects_runtime_generation_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("replacement")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    with pytest.raises(StaleSnapshotError, match="no longer live"):
        adapter.focus(node)

    assert backend.focus_calls == 1


def test_focus_acknowledgement_rejects_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("missing")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    with pytest.raises(StaleSnapshotError, match="no longer live"):
        adapter.focus(node)

    assert backend.focus_calls == 1


def test_focus_acknowledgement_rejects_ambiguous_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("ambiguous")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    with pytest.raises(AmbiguousTargetError, match="ambiguous"):
        adapter.focus(node)

    assert backend.focus_calls == 1


def test_focus_acknowledgement_times_out_without_reissuing_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch, attempts=3)
    backend = FocusAckBackend("timeout")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    with pytest.raises(StaleSnapshotError, match="timed out"):
        adapter.focus(node)

    assert backend.focus_calls == 1
    assert backend.focused_reads == 3


def test_restore_focus_waits_for_same_identity_without_reissuing_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("lag_then_success")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    assert adapter.restore_focus(node.node_id)
    assert backend.focus_calls == 1
    assert backend.focused_reads == 3


def test_restore_focus_fails_closed_on_identity_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch)
    backend = FocusAckBackend("replacement")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    assert not adapter.restore_focus(node.node_id)
    assert backend.focus_calls == 1


def test_restore_focus_timeout_does_not_reissue_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_focus_sleep(monkeypatch, attempts=3)
    backend = FocusAckBackend("timeout")
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]

    assert not adapter.restore_focus(node.node_id)
    assert backend.focus_calls == 1
    assert backend.focused_reads == 3
