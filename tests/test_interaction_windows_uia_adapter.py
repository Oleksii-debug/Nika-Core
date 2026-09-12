from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.interaction.domain import (
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
    generation: int = 1,
    role: str = "button",
    name: str = "Save",
    value: str | None = None,
    focused: bool = False,
    enabled: bool = True,
    visible: bool = True,
    patterns: tuple[str, ...] = ("Invoke",),
    bounds: tuple[int, int, int, int] = (10, 20, 100, 40),
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
        bounds=bounds,
        class_name="Button",
        framework_id="WinForm",
        patterns=patterns,
        element_generation=generation,
    )


class FakeBackend:
    def __init__(self) -> None:
        self.started = 123456789
        self.exe = r"C:\Program Files\Nika Fixture\фікстура.exe"
        self.windows = [UIAWindowRecord(100, 77, "Nika Fixture")]
        self.controls = [_record()]
        self.focused: tuple[tuple[int, ...], int] | None = None
        self.calls: list[tuple[str, tuple[int, ...], int, str | None]] = []

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

    def focused_identity(self, hwnd: int) -> tuple[tuple[int, ...], int] | None:
        assert hwnd == 100
        return self.focused

    def focus(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        assert hwnd == 100
        identity = (runtime_id, generation)
        self.focused = identity
        self.controls = [
            replace(
                control,
                focused=(control.runtime_id, control.element_generation) == identity,
            )
            for control in self.controls
        ]
        self.calls.append(("focus", runtime_id, generation, None))

    def invoke(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.calls.append(("invoke", runtime_id, generation, None))
        self.controls = [replace(control, name="Saved") for control in self.controls]

    def set_value(
        self, hwnd: int, runtime_id: tuple[int, ...], generation: int, value: str
    ) -> None:
        self.calls.append(("set_value", runtime_id, generation, value))
        self.controls = [replace(control, value=value) for control in self.controls]

    def select(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.calls.append(("select", runtime_id, generation, None))
        self.controls = [replace(control, value="selected") for control in self.controls]

    def toggle(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.calls.append(("toggle", runtime_id, generation, None))
        self.controls = [replace(control, value="on") for control in self.controls]

    def expand(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.calls.append(("expand", runtime_id, generation, None))
        self.controls = [replace(control, value="expanded") for control in self.controls]

    def collapse(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.calls.append(("collapse", runtime_id, generation, None))
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


def test_native_handle_resolves_duplicate_titles_without_first_match() -> None:
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
    assert snapshot.target.application.executable.endswith("фікстура.exe")
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


def test_node_identity_binds_runtime_id_and_element_generation() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    first = adapter.observe().controls[0]
    backend.controls = [replace(backend.controls[0], element_generation=2)]
    second = adapter.observe().controls[0]
    assert first.node_id != second.node_id
    with pytest.raises(StaleSnapshotError):
        adapter.act(first, InteractionAction.INVOKE, None)


def test_moved_resized_or_dpi_scaled_bounds_do_not_change_node_identity() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    before = adapter.observe().controls[0]
    backend.controls = [replace(backend.controls[0], bounds=(240, 360, 1440, 720))]
    after = adapter.observe().controls[0]
    assert after.node_id == before.node_id
    assert after.bounds != before.bounds


def test_duplicate_backend_records_same_runtime_generation_fail_closed() -> None:
    backend = FakeBackend()
    backend.controls = [_record(), replace(_record(), name="Other wrapper")]
    with pytest.raises(AmbiguousTargetError):
        _adapter(backend).observe()


def test_same_runtime_distinct_generations_are_distinct_nodes() -> None:
    backend = FakeBackend()
    backend.controls = [
        _record((7, 7), generation=1, name="Duplicate provider identity"),
        _record((7, 7), generation=2, name="Duplicate provider identity"),
    ]
    snapshot = _adapter(backend).observe()
    assert len(snapshot.controls) == 2
    assert snapshot.controls[0].node_id != snapshot.controls[1].node_id


def test_same_name_different_runtime_ids_remain_distinct_nodes() -> None:
    backend = FakeBackend()
    backend.controls = [_record((1, 2), name="Duplicate"), _record((9, 9), name="Duplicate")]
    snapshot = _adapter(backend).observe()
    assert len(snapshot.controls) == 2
    assert snapshot.controls[0].node_id != snapshot.controls[1].node_id


def test_exact_role_name_state_and_value_are_preserved() -> None:
    backend = FakeBackend()
    backend.controls = [
        _record(
            role="edit",
            name="Опис проблеми",
            value="точне значення",
            focused=True,
            enabled=False,
            visible=True,
            patterns=("Value", "Text"),
        )
    ]
    node = _adapter(backend).observe().controls[0]
    assert (node.role, node.name, node.value) == ("edit", "Опис проблеми", "точне значення")
    assert node.enabled is False
    assert node.visible is True
    assert node.focused is True


def test_focus_is_set_captured_verified_and_restored() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    adapter.focus(node)
    assert adapter.capture_focus() == node.node_id
    original = adapter.capture_focus()
    assert adapter.restore_focus(original) is True
    after = adapter.observe()
    focused = next(control for control in after.controls if control.node_id == node.node_id)
    assert focused.focused is True


@pytest.mark.parametrize(
    ("action", "value", "pattern", "call"),
    [
        (InteractionAction.INVOKE, None, "Invoke", "invoke"),
        (InteractionAction.SET_VALUE, "Олексій", "Value", "set_value"),
        (InteractionAction.SELECT, None, "SelectionItem", "select"),
        (InteractionAction.TOGGLE, None, "Toggle", "toggle"),
        (InteractionAction.EXPAND, None, "ExpandCollapse", "expand"),
        (InteractionAction.COLLAPSE, None, "ExpandCollapse", "collapse"),
    ],
)
def test_actions_require_and_use_supported_uia_patterns(action, value, pattern, call) -> None:
    backend = FakeBackend()
    backend.controls = [_record(patterns=(pattern,))]
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    adapter.act(node, action, value)
    assert any(item[0] == call for item in backend.calls)


def test_action_without_required_pattern_fails_before_backend_call() -> None:
    backend = FakeBackend()
    backend.controls = [_record(patterns=("Text",))]
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    with pytest.raises(UnsupportedInteractionError):
        adapter.act(node, InteractionAction.INVOKE, None)
    assert not backend.calls


def test_set_value_requires_explicit_value() -> None:
    backend = FakeBackend()
    backend.controls = [_record(patterns=("Value",))]
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


def test_pattern_capabilities_are_plain_nika_evidence() -> None:
    backend = FakeBackend()
    backend.controls = [replace(_record(), patterns=("Invoke", "Value", "Text", "Window"))]
    node = _adapter(backend).observe().controls[0]
    assert WindowsUIAInteractionAdapter.pattern_capabilities(node) == (
        "Invoke",
        "Value",
        "Text",
        "Window",
    )


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


def test_raw_uia_is_not_promoted_without_safe_measured_win() -> None:
    py = _measurement("pywinauto", 10)
    assert choose_measured_backend(py, None) == "pywinauto"
    assert choose_measured_backend(py, _measurement("raw-uia", 1, safe=False)) == "pywinauto"
    assert choose_measured_backend(py, _measurement("raw-uia", 9)) == "pywinauto"
    assert choose_measured_backend(py, _measurement("raw-uia", 7.9)) == "raw-uia"
