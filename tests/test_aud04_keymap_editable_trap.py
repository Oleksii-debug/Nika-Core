from __future__ import annotations

import re
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap

APP_JS = Path("src/nika_core/ui/web/app.js")


def _plain_binding_is_rejected(tmp_path: Path, binding: str) -> bool:
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
    keymap = Keymap(store, actions)
    try:
        keymap.set_binding("audit.probe", binding)
    except ValueError:
        return True
    return False


def _editable_unmodified_keys_are_guarded() -> bool:
    source = APP_JS.read_text(encoding="utf-8")
    marker = 'document.addEventListener("keydown", (event) => {'
    assert marker in source, "global keyboard dispatch handler is missing"
    handler = source.split(marker, 1)[1].split("});", 1)[0]
    return bool(
        re.search(
            r"isEditable\(event\.target\).*?"
            r"!event\.ctrlKey.*?!event\.altKey.*?!event\.metaKey.*?return",
            handler,
            flags=re.DOTALL,
        )
    )


@pytest.mark.parametrize("binding", ["a", "backspace", "delete", "arrowleft"])
def test_user_keymap_cannot_break_native_editing_in_editable_controls(
    tmp_path: Path,
    binding: str,
) -> None:
    rejected_by_policy = _plain_binding_is_rejected(tmp_path, binding)
    guarded_in_editable_controls = _editable_unmodified_keys_are_guarded()

    assert rejected_by_policy or guarded_in_editable_controls, (
        f"binding {binding!r} is accepted by Keymap while global keydown dispatch has no "
        "unmodified-editable guard; typing/navigation can be preventDefault()'ed and routed "
        "to a Nika action inside an input/textarea/contenteditable control"
    )
