from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.interaction import InteractionAction
from nika_core.interaction.windows_uia_adapter import (
    UIABackendMeasurement,
    UIAControlRecord,
    UIAWindowRecord,
    WindowsUIAInteractionAdapter,
    choose_measured_backend,
    measure_observation,
)


class BehavioralInt(int):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("behavioral integer equality executed")


class BehavioralText(str):
    def __repr__(self) -> str:
        raise AssertionError("behavioral text repr executed")


class BehavioralTuple(tuple):
    def __iter__(self):
        raise AssertionError("behavioral tuple iteration executed")


class CarrierBackend:
    def __init__(self) -> None:
        self.windows: object = (
            UIAWindowRecord(hwnd=100, pid=77, title="Nika Fixture"),
        )
        self.controls: object = (
            UIAControlRecord(
                runtime_id=(42,),
                automation_id="save",
                role="button",
                name="Save",
                enabled=True,
                visible=True,
                focused=False,
                value=None,
                bounds=(0, 0, 100, 30),
                patterns=("Invoke",),
            ),
        )
        self.focused: object = None
        self.effects: list[str] = []

    def process_started_ns(self, pid: int) -> int:
        return 123456789

    def executable(self, pid: int) -> str:
        return r"C:\Nika\NikaCore.exe"

    def enumerate_windows(self, pid: int):
        return self.windows

    def enumerate_controls(self, hwnd: int, view: str):
        return self.controls

    def focused_identity(self, hwnd: int):
        return self.focused

    def focus(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.effects.append("focus")

    def invoke(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.effects.append("invoke")

    def set_value(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        value: str,
    ) -> None:
        self.effects.append("set_value")

    def select(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.effects.append("select")

    def toggle(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.effects.append("toggle")

    def expand(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.effects.append("expand")

    def collapse(self, hwnd: int, runtime_id: tuple[int, ...], generation: int) -> None:
        self.effects.append("collapse")

    def guarded_focus(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        expected: UIAControlRecord,
    ) -> None:
        self.focus(hwnd, runtime_id, generation)

    def guarded_action(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        expected: UIAControlRecord,
        action: InteractionAction,
        value: str | None,
    ) -> None:
        method = {
            InteractionAction.INVOKE: self.invoke,
            InteractionAction.SET_VALUE: self.set_value,
            InteractionAction.SELECT: self.select,
            InteractionAction.TOGGLE: self.toggle,
            InteractionAction.EXPAND: self.expand,
            InteractionAction.COLLAPSE: self.collapse,
        }[action]
        if action is InteractionAction.SET_VALUE:
            assert value is not None
            method(hwnd, runtime_id, generation, value)
        else:
            method(hwnd, runtime_id, generation)


def _adapter(backend: CarrierBackend) -> WindowsUIAInteractionAdapter:
    return WindowsUIAInteractionAdapter(
        process_id=77,
        window_title="Nika Fixture",
        backend=backend,
    )


def test_window_record_behavior_is_rejected_before_identity_comparison() -> None:
    backend = CarrierBackend()
    backend.windows = (
        UIAWindowRecord(
            hwnd=100,
            pid=BehavioralInt(77),
            title="Nika Fixture",
        ),
    )

    with pytest.raises(ValueError, match="backend pid"):
        _adapter(backend).observe()

    assert backend.effects == []


def test_backend_window_collection_is_rejected_before_iteration() -> None:
    backend = CarrierBackend()
    backend.windows = BehavioralTuple(backend.windows)

    with pytest.raises(ValueError, match="windows result"):
        _adapter(backend).observe()

    assert backend.effects == []


def test_control_record_behavior_is_rejected_before_semantic_projection() -> None:
    backend = CarrierBackend()
    control = backend.controls[0]
    backend.controls = (
        replace(control, role=BehavioralText("button")),
    )

    with pytest.raises(ValueError, match="backend role"):
        _adapter(backend).observe()

    assert backend.effects == []


def test_behavioral_patterns_fail_before_action_effect() -> None:
    backend = CarrierBackend()
    adapter = _adapter(backend)
    node = adapter.observe().controls[0]
    control = backend.controls[0]
    backend.controls = (
        replace(control, patterns=BehavioralTuple(("Invoke",))),
    )

    with pytest.raises(ValueError, match="backend patterns"):
        adapter.act(node, InteractionAction.INVOKE, None)

    assert backend.effects == []


def test_malformed_focus_identity_is_rejected_before_semantic_lookup() -> None:
    backend = CarrierBackend()
    adapter = _adapter(backend)
    adapter.observe()
    backend.focused = ["not", "an exact tuple"]

    with pytest.raises(ValueError, match="focused identity"):
        adapter.capture_focus()

    assert backend.effects == []


def test_behavioral_process_identity_is_rejected_before_snapshot_binding() -> None:
    backend = CarrierBackend()

    def behavioral_start(pid: int) -> int:
        return BehavioralInt(123456789)

    backend.process_started_ns = behavioral_start  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="process start"):
        _adapter(backend).observe()

    assert backend.effects == []

class BehavioralMeasurementText(str):
    def __ne__(self, other: object) -> bool:
        raise AssertionError("behavioral backend comparison executed")


class BehavioralMeasurementInt(int):
    def __lt__(self, other: object) -> bool:
        raise AssertionError("behavioral sample comparison executed")

    def __le__(self, other: object) -> bool:
        raise AssertionError("behavioral sample comparison executed")

    def __ge__(self, other: object) -> bool:
        raise AssertionError("behavioral sample comparison executed")


class BehavioralMeasurementFloat(float):
    def __le__(self, other: object) -> bool:
        raise AssertionError("behavioral latency comparison executed")


def _measurement(
    *,
    backend: object = "pywinauto",
    sample_count: object = 5,
    latency: object = 10.0,
    patterns: object = ("Invoke", "Value"),
) -> UIABackendMeasurement:
    return UIABackendMeasurement(
        backend=backend,  # type: ignore[arg-type]
        sample_count=sample_count,  # type: ignore[arg-type]
        median_observe_ms=latency,  # type: ignore[arg-type]
        exact_identity=True,
        strict_ambiguity=True,
        focus_verified=True,
        pattern_coverage=patterns,  # type: ignore[arg-type]
    )


def test_backend_selection_rejects_behavioral_backend_name_before_comparison() -> None:
    py = _measurement(backend=BehavioralMeasurementText("pywinauto"))

    with pytest.raises(ValueError, match="backend name"):
        choose_measured_backend(py, None)


def test_backend_selection_rejects_behavioral_sample_count_before_comparison() -> None:
    py = _measurement(sample_count=BehavioralMeasurementInt(5))

    with pytest.raises(ValueError, match="sample_count"):
        choose_measured_backend(py, None)


def test_backend_selection_rejects_behavioral_pattern_tuple_before_iteration() -> None:
    py = _measurement()
    raw = _measurement(
        backend="raw-uia",
        latency=7.0,
        patterns=BehavioralTuple(("Invoke", "Value", "Text")),
    )

    with pytest.raises(ValueError, match="pattern coverage"):
        choose_measured_backend(py, raw)


def test_backend_selection_rejects_behavioral_latency_before_arithmetic() -> None:
    py = _measurement()
    raw = _measurement(
        backend="raw-uia",
        latency=BehavioralMeasurementFloat(7.0),
    )

    with pytest.raises(ValueError, match="median latency"):
        choose_measured_backend(py, raw)


@pytest.mark.parametrize("latency", [float("nan"), float("inf"), -1.0])
def test_backend_selection_rejects_nonfinite_or_negative_latency(
    latency: float,
) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        choose_measured_backend(_measurement(latency=latency), None)


def test_measure_observation_rejects_behavioral_sample_count_before_observe() -> None:
    class NeverObserve:
        def observe(self) -> None:
            raise AssertionError("observe executed")

    with pytest.raises(ValueError, match="exact integer"):
        measure_observation(
            NeverObserve(),  # type: ignore[arg-type]
            BehavioralMeasurementInt(5),
        )

