from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.interaction import (
    AmbiguousTargetError,
    InteractionAction,
    StaleSnapshotError,
    TargetNotFoundError,
    UnsupportedInteractionError,
)
from nika_core.interaction.windows_uia_adapter import (
    UIABackendMeasurement,
    UIAControlRecord,
    UIAWindowRecord,
    WindowsUIAInteractionAdapter,
    choose_measured_backend,
)


def _record(
    runtime_id: tuple[int, ...] | None = (1, 2),
    *,
    role: str = "button",
    name: str = "Save",
    value: str | None = None,
    focused: bool = False,
    enabled: bool = True,
    visible: bool = True,
) -> UIAControlRecord:
    return UIAControlRecord(
        runtime_id=runtime_id,
        automation_id="save",
        role=role,
        name=name,
        enabled=enabled,
        visible=visible,
        focused=focused,
        value=value,
        bounds=(10, 20, 100, 40),
        class_name="Button",
        framework_id="WinForm",
        patterns=("Invoke",),
    )


class FakeBackend:
    def __init__(self) -> None:
        self.started = 123456789
        self.exe = r"C:\Program Files\Nika Fixture\fixture.exe"
        self.windows = [UIAWindowRecord(100, 77, "Nika Fixture")]
        self.controls = [_record()]
        self.focused: tuple[int, ...] | None = None
        self.calls: list[tuple[str, tuple[int, ...], str | None]] = []

    def process_started_ns(self, pid: int) -> int:
        assert pid == 77
        return self.started

    def executable(self, pid: int) -> str:
        assert pid == 77
        return self.exe

    def enumerate_windows(self, pid: int) -> tuple[UIAWindowRecord, ...]:
        return tuple(self.windows)

    def enumerate_controls(self, hwnd: int, view: str) -> tuple[UIAControlRecord, ...]:
        assert hwnd in {100, 101}
        assert view in {"control", "content"}
        return tuple(self.controls)

    def focused_runtime_id(self, hwnd: int) -> tuple[int, ...] | None:
        assert hwnd == 100
        return self.focused

    def focus(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        assert hwnd == 100
        self.focused = runtime_id
        self.controls = [
            replace(control, focused=control.runtime_id == runtime_id) for control in self.controls
        ]
        self.calls.append(("focus", runtime_id, None))

    def invoke(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        self.calls.append(("invoke", runtime_id, None))
        self.controls = [replace(control, name="Saved") for control in self.controls]

    def set_value(self, hwnd: int, runtime_id: tuple[int, ...], value: str) -> None:
        self.calls.append(("set_value", runtime_id, value))
        self.controls = [replace(control, value=value) for control in self.controls]

    def select(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        self.calls.append(("select", runtime_id, None))
        self.controls = [replace(control, value="selected") for control in self.controls]

    def toggle(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        self.calls.append(("toggle", runtime_id, None))
        self.controls = [replace(control, value="on") for control in self.controls]

    def expand(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        self.calls.append(("expand", runtime_id, None))
        self.controls = [replace(control, value="expanded") for control in self.controls]

    def collapse(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        self.calls.append(("collapse", runtime_id, None))
        self.controls = [replace(control, value="collapsed") for control in self.controls]


def _adapter(backend: FakeBackend, **kwargs) -> WindowsUIAInteractionAdapter:
    return WindowsUIAInteractionAdapter(
        process_id=77,
        window_title="Nika Fixture",
        backend=backend,
        **kwargs,
    )


def test_adapter_requires_exactly_one_window_identity() -> None:
    backend = FakeBackend()
    with pytest.raises(ValueError):
        WindowsUIAInteractionAdapter(process_id=77, backend=backend)
    with pytest.raises(ValueError):
        WindowsUIAInteractionAdapter(
            process_id=77,
            window_title="Nika Fixture",
            native_handle=100,
            backend=backend,
        )


def test_exact_title_zero_match_fails_closed() -> None:
    backend = FakeBackend()
    backend.windows = [UIAWindowRecord(100, 77, "Other")]
    with pytest.raises(TargetNotFoundError):
        _adapter(backend).observe()


def test_exact_title_multiple_match_fails_closed() -> None:
    backend = FakeBackend()
    backend.windows = [
        UIAWindowRecord(100, 77, "Nika Fixture"),
        UIAWindowRecord(101, 77, "Nika Fixture"),
    ]
    with pytest.raises(AmbiguousTargetError):
        _adapter(backend).observe()


def test_native_handle_resolves_without_title_guessing() -> None:
    backend = FakeBackend()
    backend.windows = [
        UIAWindowRecord(100, 77, "Same"),
        UIAWindowRecord(101, 77, "Same"),
    ]
    adapter = WindowsUIAInteractionAdapter(process_id=77, native_handle=101, backend=backend)
    snapshot = adapter.observe()
    assert snapshot.target.window is not None
    assert snapshot.target.window.native_handle == 101


def test_snapshot_binds_pid_start_executable_hwnd_generation() -> None:
    backend = FakeBackend()
    snapshot = _adapter(backend).observe()
    assert snapshot.target.application is not None
    assert snapshot.target.application.pid == 77
    assert snapshot.target.application.process_started_ns == 123456789
    assert snapshot.target.application.executable.endswith("fixture.exe")
    assert snapshot.target.window is not None
    assert snapshot.target.window.native_handle == 100
    assert snapshot.target.window.generation == 1


def test_process_restart_is_stale_not_rebound_silently() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    adapter.observe()
    backend.started += 1
    with pytest.raises(StaleSnapshotError):
        adapter.observe()


def test_hwnd_replacement_is_stale_not_pick_new_window() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    adapter.observe()
    backend.windows = [UIAWindowRecord(101, 77, "Nika Fixture")]
    with pytest.raises(StaleSnapshotError):
        adapter.observe()


def test_controls_without_runtime_identity_never_get_positional_identity() -> None:
    backend = FakeBackend()
    backend.controls = [_record(runtime_id=None)]
    with pytest.raises(UnsupportedInteractionError):
        _adapter(backend).observe()


def test_bounds_are_evidence_only_and_do_not_affect_node_identity() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    before = adapter.observe()
    node_before = before.controls[0]
    backend.controls = [replace(backend.controls[0], bounds=(500, 600, 700, 800))]
    after = adapter.observe()
    assert after.controls[0].node_id == node_before.node_id
    assert after.controls[0].bounds != node_before.bounds


def test_focus_is_set_and_verified() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    adapter.focus(node)
    assert adapter.capture_focus() == node.node_id
    after = adapter.observe()
    assert adapter.verify(after, after, after.controls[0], InteractionAction.FOCUS, None)


def test_focus_restore_uses_semantic_runtime_identity() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    assert adapter.restore_focus(node.node_id) is True
    assert backend.focused == (1, 2)
    assert adapter.restore_focus("missing") is False


@pytest.mark.parametrize(
    ("action", "value", "call"),
    [
        (InteractionAction.INVOKE, None, "invoke"),
        (InteractionAction.SET_VALUE, "Олексій", "set_value"),
        (InteractionAction.SELECT, None, "select"),
        (InteractionAction.TOGGLE, None, "toggle"),
        (InteractionAction.EXPAND, None, "expand"),
        (InteractionAction.COLLAPSE, None, "collapse"),
    ],
)
def test_actions_map_to_non_coordinate_backend_patterns(action, value, call) -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    adapter.focus(node)
    adapter.act(node, action, value)
    assert any(item[0] == call for item in backend.calls)


def test_set_value_requires_explicit_value() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    with pytest.raises(ValueError):
        adapter.act(node, InteractionAction.SET_VALUE, None)


@pytest.mark.parametrize(("enabled", "visible"), [(False, True), (True, False)])
def test_hidden_or_disabled_action_is_blocked(enabled: bool, visible: bool) -> None:
    backend = FakeBackend()
    backend.controls = [_record(enabled=enabled, visible=visible)]
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    with pytest.raises(UnsupportedInteractionError):
        adapter.act(node, InteractionAction.INVOKE, None)


def test_old_node_is_rejected_after_new_observation_removes_it() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    old = adapter.observe().controls[0]
    backend.controls = [replace(_record((9, 9)), name="Replacement")]
    adapter.observe()
    with pytest.raises(StaleSnapshotError):
        adapter.act(old, InteractionAction.INVOKE, None)


def test_pattern_capabilities_are_nika_text_evidence_not_third_party_objects() -> None:
    backend = FakeBackend()
    backend.controls = [replace(_record(), patterns=("Invoke", "Value", "Text", "Window"))]
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    assert adapter.pattern_capabilities(node) == ("Invoke", "Value", "Text", "Window")


def _measurement(
    backend: str,
    latency: float,
    patterns: tuple[str, ...] = ("Invoke", "Value"),
    *,
    safe: bool = True,
) -> UIABackendMeasurement:
    return UIABackendMeasurement(
        backend=backend,
        sample_count=5,
        median_observe_ms=latency,
        exact_identity=safe,
        strict_ambiguity=safe,
        focus_verified=safe,
        pattern_coverage=patterns,
    )


def test_raw_uia_is_not_promoted_without_measurement() -> None:
    assert choose_measured_backend(_measurement("pywinauto", 10), None) == "pywinauto"


def test_raw_uia_is_not_promoted_if_safety_is_weaker() -> None:
    assert (
        choose_measured_backend(
            _measurement("pywinauto", 10),
            _measurement("raw-uia", 1, safe=False),
        )
        == "pywinauto"
    )


def test_raw_uia_requires_material_measured_win() -> None:
    py = _measurement("pywinauto", 10)
    assert choose_measured_backend(py, _measurement("raw-uia", 9)) == "pywinauto"
    assert choose_measured_backend(py, _measurement("raw-uia", 7.9)) == "raw-uia"
    richer = _measurement("raw-uia", 12, ("Invoke", "Value", "Text"))
    assert choose_measured_backend(py, richer) == "raw-uia"


def test_invalid_measurement_cannot_drive_backend_selection() -> None:
    py = replace(_measurement("pywinauto", 10), sample_count=2)
    with pytest.raises(ValueError):
        choose_measured_backend(py, None)
