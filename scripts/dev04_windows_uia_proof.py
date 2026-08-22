from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from nika_core.interaction import ControlLocator, InteractionAction, resolve_strict
from nika_core.interaction.windows_uia_adapter import (
    UIABackendMeasurement,
    WindowsUIAInteractionAdapter,
    choose_measured_backend,
    measure_observation,
)

TITLE = "Nika DEV04 UIA Proof"
FIXTURE = Path(__file__).parent / "fixtures" / "dev04_uia_winforms_fixture.ps1"


def observe_until_ready(adapter: WindowsUIAInteractionAdapter):
    last_error: Exception | None = None
    for _ in range(40):
        try:
            snapshot = adapter.observe()
            resolve_strict(snapshot, ControlLocator(role="edit", name="Problem description"))
            resolve_strict(snapshot, ControlLocator(role="button", name="Apply semantic action"))
            resolve_strict(snapshot, ControlLocator(role="checkbox", name="Verify semantic target"))
            return snapshot
        except Exception as exc:  # noqa: BLE001 - bounded GUI startup retry
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"WinForms UIA fixture did not become ready: {last_error!r}")


def main() -> None:
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(FIXTURE),
        ]
    )
    try:
        adapter = WindowsUIAInteractionAdapter(process_id=process.pid, window_title=TITLE)
        before = observe_until_ready(adapter)
        assert before.target.application is not None
        assert before.target.window is not None
        assert before.target.application.pid == process.pid
        assert before.target.application.process_started_ns is not None
        assert before.target.window.native_handle not in {None, 0}

        edit = resolve_strict(
            before,
            ControlLocator(role="edit", name="Problem description"),
        )
        apply_button = resolve_strict(
            before,
            ControlLocator(role="button", name="Apply semantic action"),
        )
        checkbox = resolve_strict(
            before,
            ControlLocator(role="checkbox", name="Verify semantic target"),
        )

        original_focus = adapter.capture_focus()
        adapter.focus(apply_button)
        assert adapter.capture_focus() == apply_button.node_id
        assert adapter.restore_focus(original_focus)

        adapter.focus(edit)
        adapter.act(edit, InteractionAction.SET_VALUE, "Доступність перевірено")
        after_value = adapter.observe()
        edit_after = resolve_strict(
            after_value,
            ControlLocator(role="edit", name="Problem description"),
        )
        assert edit_after.value == "Доступність перевірено"

        adapter.focus(checkbox)
        adapter.act(checkbox, InteractionAction.TOGGLE, None)
        after_toggle = adapter.observe()
        checkbox_after = resolve_strict(
            after_toggle,
            ControlLocator(role="checkbox", name="Verify semantic target"),
        )
        assert after_toggle.revision != after_value.revision
        assert "Toggle" in adapter.pattern_capabilities(checkbox_after)

        adapter.focus(apply_button)
        adapter.act(apply_button, InteractionAction.INVOKE, None)
        after_invoke = adapter.observe()
        status = resolve_strict(
            after_invoke,
            ControlLocator(name="Applied: Доступність перевірено"),
        )
        assert status.visible

        samples = measure_observation(adapter, samples=5)
        patterns = sorted(
            {
                pattern
                for control in after_invoke.controls
                for pattern in adapter.pattern_capabilities(control)
            }
        )
        baseline = UIABackendMeasurement(
            backend="pywinauto",
            sample_count=len(samples),
            median_observe_ms=statistics.median(samples),
            exact_identity=True,
            strict_ambiguity=True,
            focus_verified=True,
            pattern_coverage=tuple(patterns),
        )
        assert choose_measured_backend(baseline, None) == "pywinauto"
        print(
            json.dumps(
                {
                    "backend": "pywinauto",
                    "pid": process.pid,
                    "process_started_ns": before.target.application.process_started_ns,
                    "hwnd": before.target.window.native_handle,
                    "generation": before.target.window.generation,
                    "control_count": len(after_invoke.controls),
                    "patterns": patterns,
                    "median_observe_ms": baseline.median_observe_ms,
                    "bounds_used_for_targeting": False,
                    "coordinates_used": False,
                    "raw_uia_promoted": False,
                    "human_tested": False,
                    "nvda_verified": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
