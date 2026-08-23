from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap

APP_JS = Path("src/nika_core/ui/web/app.js")
EDITING_SENSITIVE_BINDINGS = (
    "a",
    "backspace",
    "delete",
    "arrowleft",
    "arrowright",
    "home",
    "end",
    "ctrl+backspace",
    "ctrl+delete",
    "ctrl+arrowleft",
    "ctrl+arrowright",
    "shift+arrowleft",
    "shift+arrowright",
)


def _keymap(tmp_path: Path) -> Keymap:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = ActionRegistry()
    actions.register(
        ActionDefinition(
            action_id="audit.probe",
            label="AUD04 probe",
            category="audit",
            default_binding="ctrl+shift+f12",
        )
    )
    return Keymap(store, actions)


def _set_binding_is_rejected(tmp_path: Path, binding: str) -> bool:
    keymap = _keymap(tmp_path)
    try:
        keymap.set_binding("audit.probe", binding)
    except ValueError:
        return True
    return False


def _import_binding_is_rejected(tmp_path: Path, binding: str) -> bool:
    keymap = _keymap(tmp_path)
    payload = json.dumps(
        {
            "format_version": keymap.FORMAT_VERSION,
            "bindings": {"audit.probe": binding},
        }
    )
    try:
        keymap.import_json(payload)
    except ValueError:
        return True
    return False


def _editable_controls_are_excluded_from_global_dispatch() -> bool:
    source = APP_JS.read_text(encoding="utf-8")
    marker = 'document.addEventListener("keydown", (event) => {'
    end_marker = 'window.addEventListener("pywebviewready"'
    assert marker in source, "global keyboard dispatch handler is missing"
    assert end_marker in source, "could not bound global keyboard dispatch handler"
    handler = source.split(marker, 1)[1].split(end_marker, 1)[0]
    prevent_default = "event.preventDefault();"
    assert prevent_default in handler, "global keyboard dispatch no longer exposes preventDefault path"
    before_prevent_default = handler.split(prevent_default, 1)[0]
    return bool(
        re.search(
            r"if\s*\(\s*isEditable\(event\.target\)\s*\)\s*(?:\{\s*)?return;?",
            before_prevent_default,
            flags=re.DOTALL,
        )
    )


@pytest.mark.parametrize("binding", EDITING_SENSITIVE_BINDINGS)
def test_user_keymap_cannot_break_native_editing_in_editable_controls(
    tmp_path: Path,
    binding: str,
) -> None:
    editable_dispatch_blocked = _editable_controls_are_excluded_from_global_dispatch()
    set_rejected = _set_binding_is_rejected(tmp_path / "set", binding)
    import_rejected = _import_binding_is_rejected(tmp_path / "import", binding)

    assert editable_dispatch_blocked or (set_rejected and import_rejected), (
        f"editing-sensitive binding {binding!r} is accepted by a keymap write path while global "
        "keydown dispatch can still reach preventDefault() inside an input/textarea/"
        "contenteditable control"
    )