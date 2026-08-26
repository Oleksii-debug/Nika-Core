from __future__ import annotations

from pathlib import Path

_WEB_ROOT = Path(__file__).parents[1] / "src" / "nika_core" / "ui" / "web"


def _source() -> str:
    return (_WEB_ROOT / "app.js").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_state_failure_hides_stale_product_project_projection() -> None:
    source = _source()
    unavailable = _between(
        source,
        "function renderProductProjectUnavailable(message) {",
        "function renderProductProject(project) {",
    )
    refresh = _between(
        source,
        "async function refreshState() {",
        "async function dispatch(actionId, trigger = null) {",
    )

    assert 'productProjectEmpty.hidden = false;' in unavailable
    assert 'productProjectSummary.hidden = true;' in unavailable
    assert 'clearProductProjectFields();' in unavailable
    assert refresh.count(
        'renderProductProjectUnavailable("Стан поточного ProductProject недоступний.");'
    ) == 3
    assert "response = await globalThis.pywebview.api.get_state();" in refresh
    assert "catch (error)" in refresh
    assert "if (!response.ok)" in refresh


def test_initialization_requires_a_successful_initial_state_snapshot() -> None:
    source = _source()
    initialize = _between(
        source,
        "async function initializeBridge() {",
        'document.getElementById("keymap-export")',
    )

    state_refresh = "const stateReady = await refreshState();"
    state_guard = 'if (!stateReady) throw new Error("Desktop state bridge unavailable");'
    ready_marker = 'document.documentElement.dataset.nikaReady = "true";'
    assert state_refresh in initialize
    assert state_guard in initialize
    assert initialize.index(state_refresh) < initialize.index(state_guard)
    assert initialize.index(state_guard) < initialize.index(ready_marker)


def test_repair_preserves_existing_keymap_binding_contract() -> None:
    source = _source()

    assert 'action.may_be_unbound ? "Зберегти / очистити" : "Зберегти"' in source
    assert "may_beUnbound" not in source
