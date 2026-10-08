from __future__ import annotations

from pathlib import Path

_WEB_ROOT = Path(__file__).parents[1] / "src" / "nika_core" / "ui" / "web"


def _source(name: str) -> str:
    return (_WEB_ROOT / name).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_product_project_surface_uses_native_semantic_structure() -> None:
    html = _source("index.html")
    assert '<a href="#product-project-heading">ProductProject</a>' in html
    assert '<section aria-labelledby="product-project-heading">' in html
    assert 'id="product-project-heading" tabindex="-1"' in html
    assert (
        '<dl id="product-project-summary" aria-label="Поточний ProductProject" hidden>'
        in html
    )
    for field_id in (
        "product-project-title",
        "product-project-id",
        "product-project-goal",
        "product-project-state",
        "product-project-spec-version",
        "product-project-blocker-count",
        "product-project-status-count",
        "product-project-decision-count",
        "product-project-statuses-heading",
        "product-project-statuses-empty",
        "product-project-statuses-list",
        "product-project-statuses-truncated",
        "product-project-operator-heading",
        "product-project-operator-project",
        "product-project-operator-work",
        "product-project-operator-owner",
        "product-project-operator-state",
        "product-project-operator-blocker",
        "product-project-operator-candidate",
        "product-project-operator-test",
        "product-project-operator-qa",
        "product-project-operator-integration",
        "product-project-operator-next",
        "product-project-decision-heading",
        "product-project-decision-id",
        "product-project-decision-title",
        "product-project-decision-question",
        "product-project-decision-risk",
        "product-project-decision-state",
    ):
        assert f'id="{field_id}"' in html


def test_product_project_renderer_tracks_bounded_bridge_projection() -> None:
    source = _source("app.js")
    render_block = _between(
        source,
        "function renderProductProject(project) {",
        "async function refreshState(",
    )
    assert "if (project == null)" in render_block
    assert "if (!validProductProject(project))" in render_block
    assert "productProjectEmpty.hidden = true;" in render_block
    assert "productProjectSummary.hidden = false;" in render_block
    assert "node.textContent = String(project[field]);" in render_block
    assert "renderProductProjectStatuses(project);" in render_block
    assert "renderProductProjectOperator(project.operator);" in render_block
    operator_renderer = _between(
        source,
        "function renderProductProjectOperator(operator) {",
        "function renderProductProject(project) {",
    )
    assert "productProjectOperatorFields[field].textContent = operator[field];" in operator_renderer
    assert "innerHTML" not in operator_renderer
    assert "productProjectDecisionFields.question.textContent = decision.question;" in render_block
    assert 'productProjectDecisionFields.state.textContent = "Очікує рішення";' in render_block
    assert "productProjectDecision.hidden = false;" in render_block
    assert "innerHTML" not in render_block
    assert "renderProductProject(state.product_project ?? null);" in source


def test_product_project_renderer_rejects_malformed_snapshot_fail_closed() -> None:
    source = _source("app.js")
    validator = _between(
        source,
        "function validProductProject(project) {",
        "function clearProductProjectFields() {",
    )
    assert 'const stringFields = ["title", "project_id", "goal", "state"];' in validator
    assert "typeof project[field] !== \"string\" || !project[field].trim()" in validator
    assert "!Number.isInteger(project.spec_version) || project.spec_version < 1" in validator
    assert 'const countFields = ["blocker_count", "status_count", "decision_count"];' in validator
    assert "Number.isInteger(project[field]) && project[field] >= 0" in validator
    assert "Array.isArray(project.status_items)" in validator
    assert "project.status_items.length > 24" in validator
    assert "project.status_items.every(validProductStatusItem)" in validator
    assert "project.status_items_truncated" in validator
    assert 'hasOwnProperty.call(project, "current_decision")' in validator
    assert 'hasOwnProperty.call(project, "operator")' in validator
    assert "validProductOperator(project.operator)" in validator
    operator_validator = _between(
        source,
        "function validProductOperator(operator) {",
        "function validProductProject(project) {",
    )
    assert "Object.keys(operator)" in operator_validator
    assert "keys.length !== productProjectOperatorFieldNames.length" in operator_validator
    assert "Object.prototype.hasOwnProperty.call(operator, field)" in operator_validator
    assert 'typeof operator[field] === "string"' in operator_validator
    assert "operator[field].length <= 4000" in operator_validator
    status_validator = _between(
        source,
        "function validProductStatusItem(item) {",
        "function validProductDecision(decision) {",
    )
    assert "productStatusKindLabels" in status_validator
    assert 'typeof item.detail === "string"' in status_validator
    decision_validator = _between(
        source,
        "function validProductDecision(decision) {",
        "function validProductProject(project) {",
    )
    assert 'decision.state !== "pending"' in decision_validator
    assert "decision.risk_level >= 0" in decision_validator
    assert "decision.risk_level <= 4" in decision_validator

    renderer = _between(
        source,
        "function renderProductProject(project) {",
        "async function refreshState(",
    )
    assert "Стан поточного ProductProject недоступний або пошкоджений." in renderer
    assert "productProjectSummary.hidden = true;" in renderer
    assert "clearProductProjectFields();" in renderer
    assert "Некоректний bounded ProductProject state відхилено інтерфейсом." in renderer


def test_product_project_renderer_does_not_expand_authority_or_secret_fields() -> None:
    source = _source("app.js")
    field_block = _between(
        source,
        "const productProjectFields = Object.freeze({",
        "let actions = [];",
    )
    for field in (
        "title",
        "project_id",
        "goal",
        "state",
        "spec_version",
        "blocker_count",
        "status_count",
        "decision_count",
    ):
        assert f"{field}:" in field_block
    for forbidden in (
        "evidence_refs",
        "credential_refs",
        "authorization_ref",
        "provider_session",
        "protected_store_handle",
    ):
        assert forbidden not in field_block

    html = _source("index.html")
    decision_block = _between(
        html,
        '<div id="product-project-decision" hidden>',
        "</section>",
    )
    assert '<dl aria-label="Поточне рішення ProductProject">' in decision_block
    assert "<button" not in decision_block

    status_block = _between(
        html,
        '<div id="product-project-statuses" hidden>',
        '<div id="product-project-operator" hidden>',
    )
    assert '<ul id="product-project-statuses-list"' in status_block
    assert 'aria-label="Статусні записи ProductProject"' in status_block
    assert "<button" not in status_block

    operator_block = _between(
        html,
        '<div id="product-project-operator" hidden>',
        '<div id="product-project-decision" hidden>',
    )
    assert '<dl aria-label="Поточний операторський стан Product Factory">' in operator_block
    assert 'id="product-project-operator-heading" tabindex="-1"' in operator_block
    assert "<button" not in operator_block


def test_product_project_refresh_preserves_backend_focus_precedence() -> None:
    source = _source("app.js")
    dispatch = _between(
        source,
        "async function dispatch(actionId, trigger = null) {",
        "async function refreshKeymap() {",
    )
    assert dispatch.index("const focusId = result.focus_id ||") < dispatch.index(
        "await refreshState();"
    )
    assert 'focusElementById("product-project-heading")' not in dispatch
