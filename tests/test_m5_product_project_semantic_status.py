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
    ):
        assert f'id="{field_id}"' in html


def test_product_project_renderer_tracks_bounded_bridge_projection() -> None:
    source = _source("app.js")
    render_block = _between(
        source,
        "function renderProductProject(project) {",
        "async function refreshState() {",
    )
    assert "project !== null" in render_block
    assert '!Array.isArray(project)' in render_block
    assert "productProjectEmpty.hidden = visible;" in render_block
    assert "productProjectSummary.hidden = !visible;" in render_block
    assert "node.textContent = visible" in render_block
    assert "innerHTML" not in render_block
    assert "renderProductProject(state.product_project ?? null);" in source


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
