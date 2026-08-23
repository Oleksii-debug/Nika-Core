from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from nika_core.interaction import (
    ControlLocator,
    InteractionAction,
    StaleSnapshotError,
    resolve_strict,
)
from nika_core.interaction.windows_uia_adapter import (
    PywinautoUIABackend,
    UIABackendMeasurement,
    WindowsUIAInteractionAdapter,
    choose_measured_backend,
    measure_observation,
)

TITLE = "Nika DEV04 UIA Proof"
FIXTURE = Path(__file__).parent / "fixtures" / "dev04_uia_winforms_fixture.ps1"


def _resolve(snapshot, *, role: str | None = None, name: str):
    return resolve_strict(snapshot, ControlLocator(role=role, name=name))


def observe_until_ready(adapter: WindowsUIAInteractionAdapter):
    last_error: Exception | None = None
    for _ in range(40):
        try:
            snapshot = adapter.observe()
            _resolve(snapshot, role="edit", name="Problem description")
            _resolve(snapshot, role="button", name="Apply semantic action")
            _resolve(snapshot, role="checkbox", name="Verify semantic target")
            _resolve(snapshot, role="button", name="Move and resize window")
            _resolve(snapshot, role="button", name="Replace semantic target")
            _resolve(snapshot, role="button", name="Replaceable semantic action")
            return snapshot
        except Exception as exc:  # noqa: BLE001 - bounded GUI startup observation
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
    backend = PywinautoUIABackend()
    try:
        adapter = WindowsUIAInteractionAdapter(
            process_id=process.pid,
            window_title=TITLE,
            backend=backend,
        )
        before = observe_until_ready(adapter)
        assert before.target.application is not None
        assert before.target.window is not None
        assert before.target.application.pid == process.pid
        assert before.target.application.process_started_ns is not None
        assert before.target.application.executable.lower().endswith("powershell.exe")
        assert before.target.window.native_handle not in {None, 0}
        assert before.target.window.generation == 1
        collision_runtime_ids = backend.last_duplicate_runtime_ids
        assert collision_runtime_ids, (
            "WinForms fixture did not reproduce the duplicate RuntimeId family "
            "that previously caused duplicate UIA semantic node identity"
        )

        edit = _resolve(before, role="edit", name="Problem description")
        apply_button = _resolve(before, role="button", name="Apply semantic action")
        checkbox = _resolve(before, role="checkbox", name="Verify semantic target")
        move = _resolve(before, role="button", name="Move and resize window")
        replace_control = _resolve(before, role="button", name="Replace semantic target")
        replaceable = _resolve(before, role="button", name="Replaceable semantic action")

        assert edit.enabled and edit.visible
        assert "Value" in adapter.pattern_capabilities(edit)
        assert "Invoke" in adapter.pattern_capabilities(apply_button)
        assert "Toggle" in adapter.pattern_capabilities(checkbox)

        original_focus = adapter.capture_focus()
        adapter.focus(apply_button)
        assert adapter.capture_focus() == apply_button.node_id
        assert adapter.restore_focus(original_focus)

        adapter.focus(edit)
        adapter.act(edit, InteractionAction.SET_VALUE, "Доступність перевірено")
        after_value = adapter.observe()
        edit_after = _resolve(after_value, role="edit", name="Problem description")
        assert edit_after.value == "Доступність перевірено"

        checkbox = _resolve(after_value, role="checkbox", name="Verify semantic target")
        adapter.focus(checkbox)
        adapter.act(checkbox, InteractionAction.TOGGLE, None)
        after_toggle = adapter.observe()
        checkbox_after = _resolve(
            after_toggle, role="checkbox", name="Verify semantic target"
        )
        assert after_toggle.revision != after_value.revision
        assert "Toggle" in adapter.pattern_capabilities(checkbox_after)

        apply_button = _resolve(after_toggle, role="button", name="Apply semantic action")
        adapter.focus(apply_button)
        adapter.act(apply_button, InteractionAction.INVOKE, None)
        after_invoke = adapter.observe()
        status = _resolve(after_invoke, role="text", name="Applied: Доступність перевірено")
        assert status.visible

        edit_before_move = _resolve(after_invoke, role="edit", name="Problem description")
        move = _resolve(after_invoke, role="button", name="Move and resize window")
        old_bounds = edit_before_move.bounds
        adapter.act(move, InteractionAction.INVOKE, None)
        after_move = adapter.observe()
        edit_after_move = _resolve(after_move, role="edit", name="Problem description")
        assert edit_after_move.node_id == edit_before_move.node_id
        assert edit_after_move.bounds != old_bounds
        assert _resolve(after_move, role="text", name="Moved and resized").visible

        replaceable = _resolve(after_move, role="button", name="Replaceable semantic action")
        replace_control = _resolve(after_move, role="button", name="Replace semantic target")
        adapter.act(replace_control, InteractionAction.INVOKE, None)
        after_replace = adapter.observe()
        replacement = _resolve(
            after_replace, role="button", name="Replaceable semantic action"
        )
        assert replacement.node_id != replaceable.node_id
        try:
            adapter.act(replaceable, InteractionAction.INVOKE, None)
        except StaleSnapshotError:
            pass
        else:
            raise AssertionError("replaced UIA control retained stale action authority")

        samples = measure_observation(adapter, samples=5)
        final = adapter.observe()
        patterns = sorted(
            {
                pattern
                for control in final.controls
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
                    "executable": before.target.application.executable,
                    "hwnd": before.target.window.native_handle,
                    "window_generation": before.target.window.generation,
                    "control_count": len(final.controls),
                    "patterns": patterns,
                    "median_observe_ms": baseline.median_observe_ms,
                    "duplicate_runtime_ids_disambiguated_by_generation": [
                        list(item) for item in collision_runtime_ids
                    ],
                    "moved_resized_identity_stable": True,
                    "dpi_position_used_for_targeting": False,
                    "coordinates_used": False,
                    "stale_replacement_rejected": True,
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
