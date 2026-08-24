from __future__ import annotations

from pathlib import Path

_WEB_ROOT = Path(__file__).parents[1] / "src" / "nika_core" / "ui" / "web"


def _app_source() -> str:
    return (_WEB_ROOT / "app.js").read_text(encoding="utf-8")


def _index_source() -> str:
    return (_WEB_ROOT / "index.html").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_keymap_row_controls_bind_semantics_to_stable_action_ids() -> None:
    source = _app_source()
    assert (
        'return `keymap-${control}-${encodeURIComponent(String(actionId))}`;'
        in source
    )
    assert 'return `${action.label} (${action.action_id})`;' in source
    assert "const accessibleActionLabel = keymapAccessibleActionLabel(action);" in source
    assert 'input.id = keymapControlId(action.action_id, "binding");' in source
    assert 'const saveFocusId = keymapControlId(action.action_id, "save");' in source
    assert 'const restoreFocusId = keymapControlId(action.action_id, "restore");' in source
    assert 'save.id = saveFocusId;' in source
    assert 'restore.id = restoreFocusId;' in source
    assert '`Комбінація для ${accessibleActionLabel}`' in source
    assert '`Зберегти або очистити комбінацію для ${accessibleActionLabel}`' in source
    assert '`Зберегти комбінацію для ${accessibleActionLabel}`' in source
    assert (
        '`Відновити комбінацію за замовчуванням для ${accessibleActionLabel}`'
        in source
    )


def test_keymap_save_restores_semantic_focus_after_table_rebuild() -> None:
    source = _app_source()
    save_block = _between(
        source,
        'save.addEventListener("click", async () => {',
        'const restore = document.createElement("button");',
    )
    refresh = save_block.index("await refreshKeymap();")
    restore_focus = save_block.index("focusElementById(saveFocusId);")
    assert refresh < restore_focus
    assert "querySelector" not in save_block


def test_keymap_restore_default_restores_semantic_focus_after_table_rebuild() -> None:
    source = _app_source()
    restore_block = _between(
        source,
        'restore.addEventListener("click", async () => {',
        'controlCell.append(save, document.createTextNode(" "), restore);',
    )
    refresh = restore_block.index("await refreshKeymap();")
    restore_focus = restore_block.index("focusElementById(restoreFocusId);")
    assert refresh < restore_focus
    assert "querySelector" not in restore_block


def test_rejected_task_create_returns_focus_to_command_editor() -> None:
    html = _index_source()
    source = _app_source()
    assert (
        'data-action-id="task.create" data-error-focus-target="command-input"'
        in html
    )
    dispatch_block = _between(
        source,
        "async function dispatch(actionId, trigger = null) {",
        "async function refreshKeymap() {",
    )
    assert (
        "result.focus_id || (failed ? trigger?.dataset?.errorFocusTarget : "
        "trigger?.dataset?.focusTarget)"
    ) in dispatch_block
    assert "focusElementById(focusId);" in dispatch_block
    assert "querySelector" not in dispatch_block
