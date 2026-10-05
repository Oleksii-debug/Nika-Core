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
    TargetNotFoundError,
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


def focus_until_verified(
    adapter: WindowsUIAInteractionAdapter,
    node,
    *,
    attempts: int = 20,
    delay_seconds: float = 0.05,
) -> None:
    """Issue one focus effect, then boundedly re-observe exact authority.

    Production ``focus`` already waits read-only for the authoritative focused
    AutomationElement to bind to the exact RuntimeId/generation. Hosted providers
    may omit ``CurrentHasKeyboardFocus`` from tree snapshots even after successful
    ``SetFocus``. This outer proof therefore re-observes the semantic identity and
    independently requires ``capture_focus()`` to keep resolving that same exact
    identity; it never reissues the effect or treats a tree focus flag as authority.
    """

    adapter.focus(node)
    for attempt in range(attempts):
        snapshot = adapter.observe()
        current = _resolve(snapshot, role=node.role, name=node.name)
        if current.node_id != node.node_id:
            raise StaleSnapshotError(
                "focus target identity changed during bounded verification"
            )
        if adapter.capture_focus() == node.node_id:
            return
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise AssertionError(
        f"exact UIA focus did not remain observable within {attempts} read-only attempts"
    )


def invoke_and_observe_until(
    adapter: WindowsUIAInteractionAdapter,
    node,
    witness,
    *,
    attempts: int = 40,
    delay_seconds: float = 0.05,
):
    """Issue one Invoke effect, then wait only through read-only observations.

    UIA Invoke is asynchronous: return from the provider is not application-level
    completion evidence. The action is never replayed here. Every observation must
    retain the exact semantic action identity and Invoke capability; only the
    caller-supplied semantic witness may end the bounded wait successfully.
    """

    if attempts <= 0:
        raise ValueError("attempts must be positive")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")

    adapter.act(node, InteractionAction.INVOKE, None)
    last_missing: TargetNotFoundError | None = None
    for attempt in range(attempts):
        snapshot = adapter.observe()
        current = _resolve(snapshot, role=node.role, name=node.name)
        if current.node_id != node.node_id:
            raise StaleSnapshotError(
                "invoke target identity changed during completion observation"
            )
        if current.enabled != node.enabled or current.visible != node.visible:
            raise StaleSnapshotError(
                "invoke target actionability changed during completion observation"
            )
        if "Invoke" not in adapter.pattern_capabilities(current):
            raise StaleSnapshotError(
                "invoke target lost Invoke authority during completion observation"
            )
        try:
            if witness(snapshot):
                return snapshot
        except TargetNotFoundError as exc:
            last_missing = exc
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)

    detail = f": {last_missing!r}" if last_missing is not None else ""
    raise AssertionError(
        f"UIA Invoke semantic witness did not appear within {attempts} observations{detail}"
    )


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

        unaddressable_count = backend.last_unaddressable_count
        collision_runtime_ids = backend.last_duplicate_runtime_ids

        edit = _resolve(before, role="edit", name="Problem description")
        apply_button = _resolve(before, role="button", name="Apply semantic action")
        checkbox = _resolve(before, role="checkbox", name="Verify semantic target")
        assert edit.enabled and edit.visible
        assert "Value" in adapter.pattern_capabilities(edit)
        assert "Invoke" in adapter.pattern_capabilities(apply_button)
        assert "Toggle" in adapter.pattern_capabilities(checkbox)

        original_focus = adapter.capture_focus()
        focus_until_verified(adapter, apply_button)
        assert adapter.restore_focus(original_focus)

        focus_until_verified(adapter, edit)
        adapter.act(edit, InteractionAction.SET_VALUE, "Доступність перевірено")
        after_value = adapter.observe()
        edit_after = _resolve(after_value, role="edit", name="Problem description")
        assert edit_after.value == "Доступність перевірено"

        checkbox = _resolve(after_value, role="checkbox", name="Verify semantic target")
        focus_until_verified(adapter, checkbox)
        adapter.act(checkbox, InteractionAction.TOGGLE, None)
        after_toggle = adapter.observe()
        checkbox_after = _resolve(
            after_toggle, role="checkbox", name="Verify semantic target"
        )
        toggle_status = _resolve(after_toggle, role="text", name="Toggle state: Checked")
        assert checkbox_after.node_id == checkbox.node_id
        assert toggle_status.visible
        assert after_toggle.revision != after_value.revision
        assert "Toggle" in adapter.pattern_capabilities(checkbox_after)

        apply_button = _resolve(after_toggle, role="button", name="Apply semantic action")
        focus_until_verified(adapter, apply_button)
        after_invoke = invoke_and_observe_until(
            adapter,
            apply_button,
            lambda snapshot: _resolve(
                snapshot,
                role="text",
                name="Applied: Доступність перевірено",
            ).visible,
        )

        edit_before_move = _resolve(after_invoke, role="edit", name="Problem description")
        move = _resolve(after_invoke, role="button", name="Move and resize window")
        old_bounds = edit_before_move.bounds

        def move_witness(snapshot) -> bool:
            current_edit = _resolve(snapshot, role="edit", name="Problem description")
            if current_edit.node_id != edit_before_move.node_id:
                raise StaleSnapshotError(
                    "edit identity changed during move/resize completion observation"
                )
            return (
                current_edit.bounds != old_bounds
                and _resolve(
                    snapshot,
                    role="text",
                    name="Moved and resized",
                ).visible
            )

        after_move = invoke_and_observe_until(adapter, move, move_witness)
        edit_after_move = _resolve(after_move, role="edit", name="Problem description")
        assert edit_after_move.node_id == edit_before_move.node_id
        assert edit_after_move.bounds != old_bounds

        replaceable = _resolve(after_move, role="button", name="Replaceable semantic action")
        replace_control = _resolve(after_move, role="button", name="Replace semantic target")

        def replacement_witness(snapshot) -> bool:
            replacement = _resolve(
                snapshot,
                role="button",
                name="Replaceable semantic action",
            )
            return (
                replacement.node_id != replaceable.node_id
                and _resolve(snapshot, role="text", name="Target replaced").visible
            )

        after_replace = invoke_and_observe_until(
            adapter,
            replace_control,
            replacement_witness,
        )
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
                    "provider_anomaly_observation_required": False,
                    "unaddressable_runtime_id_elements_observed": unaddressable_count,
                    "duplicate_runtime_ids_observed": [
                        list(item) for item in collision_runtime_ids
                    ],
                    "provider_anomaly_contract_proven_by_deterministic_tests": True,
                    "moved_resized_identity_stable": True,
                    "toggle_effect_semantically_observed": True,
                    "dpi_position_used_for_targeting": False,
                    "coordinates_used": False,
                    "stale_replacement_rejected": True,
                    "bounded_exact_focus_verification": True,
                    "single_focus_effect_per_verification": True,
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
