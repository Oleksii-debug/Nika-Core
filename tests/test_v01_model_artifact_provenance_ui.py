from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_model_artifact_provenance_ui_is_semantic_and_not_a_live_region() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")

    assert 'id="model-artifact-panel" hidden' in html
    assert '<h3 id="model-artifact-heading">Походження вибраної моделі</h3>' in html
    assert 'id="model-artifact-status"' in html
    assert '<dl id="model-artifact-summary" hidden>' in html
    for field_id in (
        "model-artifact-kind",
        "model-artifact-version",
        "model-artifact-integrity",
        "model-artifact-sha256",
        "model-artifact-descriptor-digest",
        "model-artifact-source",
        "model-artifact-license",
        "model-artifact-size",
        "model-artifact-capabilities",
        "model-artifact-resources",
    ):
        assert f'id="{field_id}"' in html
    assert (
        'id="model-route-kind" '
        'aria-describedby="model-settings-help model-settings-status model-artifact-status"'
        in html
    )
    assert (
        'id="model-name" type="text" spellcheck="false" autocomplete="off" '
        'aria-describedby="model-settings-help model-artifact-status"'
        in html
    )
    assert html.count('aria-live="') == 1


def test_model_artifact_renderer_uses_text_only_and_truth_preserving_language() -> None:
    app = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    start = app.index("  function renderModelArtifact(")
    end = app.index("\n  function validModelSnapshot(", start)
    body = app[start:end]

    assert "innerHTML" not in body
    assert ".textContent =" in body
    assert "немає зареєстрованих відомостей" in body
    assert "пошкоджені або несумісні" in body
    assert "не є доказом фактичного завантаження або запуску цих байтів" in body
    assert 'artifact.status === "registered"' not in body
    assert 'artifact.status === "unregistered"' in body
    assert 'artifact.status === "invalid"' in body


def test_model_artifact_snapshot_validation_rejects_unbounded_or_ambiguous_carriers() -> None:
    app = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    start = app.index("  function validModelArtifactSnapshot(")
    end = app.index("\n  function renderModelArtifact(", start)
    body = app[start:end]

    assert "value.length <= 128" in body
    assert "/^[0-9a-f]{64}$/" in body
    assert "/^[1-9][0-9]{0,18}$/" in body
    assert 'artifact.integrity_basis === "sha256"' in body
    assert "resources.cpu_architectures" in body
