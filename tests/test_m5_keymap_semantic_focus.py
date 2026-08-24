from __future__ import annotations

from pathlib import Path


def _app_source() -> str:
    return (
        Path(__file__).parents[1] / "src" / "nika_core" / "ui" / "web" / "app.js"
    ).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_keymap_row_controls_have_action_specific_accessible_names_and_ids() -> None:
    source = _app_source()
    assert 'return `keymap-${control}-${actionId}`;' in source
    assert 'input.id = keymapControlId(action.action_id, "binding");' in source
    assert 'const saveFocusId = keymapControlId(action.action_id, "save");' in source
    assert 'const restoreFocusId = keymapControlId(action.action_id, "restore");' in source
    assert 'save.id = saveFocusId;' in source
    assert 'restore.id = restoreFocusId;' in source
    assert '`Зберегти або очистити комбінацію для ${action.label}`' in source
    assert '`Зберегти комбінацію для ${action.label}`' in source
    assert '`Відновити комбінацію за замовчуванням для ${action.label}`' in source


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
