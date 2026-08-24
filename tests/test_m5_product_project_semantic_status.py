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


def test_product_project_has_persistent_native_semantic_surface() -> None:
    html = _index_source()
    assert '<a href="#product-project-heading">ProductProject</a>' in html
    assert '<section aria-labelledby="product-project-heading">' in html
    assert '<h2 id="product-project-heading" tabindex="-1">' in html
    assert '<p id="product-project-empty">' in html
    assert '<dl id="product-project-details" hidden>' in html
    for element_id in (
        "product-project-title",
        "product-project-id",
        "product-project-goal",
        "product-project-spec-version",
        "product-project-state",
        "product-project-blockers",
        "product-project-status-count",
        "product-project-decision-count",
    ):
        assert f'id="{element_id}"' in html


def test_product_project_renderer_consumes_bounded_state_after_refresh() -> None:
    source = _app_source()
    refresh = _between(
        source,
        "async function refreshState() {",
        "async function dispatch(actionId, trigger = null) {",
    )
    assert "renderProductProject(state.product_project ?? null);" in refresh

    renderer = _between(
        source,
        "function renderProductProject(project) {",
        "async function refreshState() {",
    )
    for field in (
        "title",
        "project_id",
        "goal",
        "spec_version",
        "state",
        "blocker_count",
        "status_count",
        "decision_count",
    ):
        assert f"project.{field}" in renderer
    assert ".textContent =" in renderer
    assert "innerHTML" not in renderer
    assert "insertAdjacentHTML" not in renderer


def test_product_project_renderer_rejects_malformed_snapshot_fail_closed() -> None:
    source = _app_source()
    validator = _between(
        source,
        "function validProductProject(project) {",
        "function renderProductProject(project) {",
    )
    assert 'const stringFields = ["title", "project_id", "goal", "state"];' in validator
    assert (
        'const integerFields = ["spec_version", "blocker_count", "status_count", '
        '"decision_count"];'
    ) in validator
    assert "Number.isInteger(project[field]) && project[field] >= 0" in validator

    renderer = _between(
        source,
        "function renderProductProject(project) {",
        "async function refreshState() {",
    )
    assert "if (!validProductProject(project))" in renderer
    assert 'productProjectDetails.hidden = true;' in renderer
    assert 'productProjectEmpty.hidden = false;' in renderer
    assert "Некоректний bounded ProductProject state відхилено інтерфейсом." in renderer


def test_product_project_refresh_does_not_override_owner_success_focus_contract() -> None:
    source = _app_source()
    dispatch = _between(
        source,
        "async function dispatch(actionId, trigger = null) {",
        "async function refreshKeymap() {",
    )
    refresh_index = dispatch.index("await refreshState();")
    focus_index = dispatch.index("const focusId = result.focus_id ||")
    assert refresh_index < focus_index
    assert 'focusElementById("product-project-heading")' not in dispatch


def test_product_project_frontend_does_not_request_authority_or_secret_fields() -> None:
    source = _app_source()
    for forbidden in (
        "credential_refs",
        "authorization_ref",
        "provider_session",
        "protected_store_handle",
        "evidence_refs",
    ):
        assert forbidden not in source
