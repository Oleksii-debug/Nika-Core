from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]

def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")

def test_packaged_local_repository_controls_are_semantic_and_keyboard_native() -> None:
    html = _text("src/nika_core/ui/web/index.html")
    assert '<label for="product-factory-local-repository-select">' in html
    assert 'id="product-factory-local-repository-select"' in html
    assert '<label for="product-factory-local-repository-root">' in html
    assert 'id="product-factory-local-repository-root"' in html
    assert 'maxlength="32767"' in html
    assert 'data-action-id="product.factory.local_repository.bind"' in html
    assert 'data-action-id="product.factory.local_repository.unbind"' in html
    assert 'aria-describedby="product-factory-local-repository-help product-factory-local-repository-status"' in html

def test_packaged_local_repository_ui_uses_projected_versions_and_fail_closed_state() -> None:
    javascript = _text("src/nika_core/ui/web/app.js")
    for fragment in (
        "function renderProductFactoryLocalRepositories(snapshot)",
        "productFactoryLocalRepositoryVersions = new Map()",
        'actionId === "product.factory.local_repository.bind"',
        'actionId === "product.factory.local_repository.unbind"',
        "payload.expected_binding_version",
        "payload.root_path",
        "item.binding_status",
        "прив’язка недійсна",
        "renderProductFactoryLocalRepositories(null)",
        "state.product_factory_local_repositories ?? null",
    ):
        assert fragment in javascript

def test_packaged_bridge_registers_operator_actions_and_redacted_state() -> None:
    windows = _text("scripts/nika_windows.py")
    actions = _text("src/nika_core/kernel/default_actions.py")
    assert "PackagedLocalRepositoryOperator" in windows
    assert 'state["product_factory_local_repositories"]' in windows
    assert '"product.factory.local_repository.bind":' in windows
    assert '"product.factory.local_repository.unbind":' in windows
    assert '"product.factory.local_repository.bind"' in actions
    assert '"product.factory.local_repository.unbind"' in actions


def test_windows_uia_release_proof_covers_local_repository_operator_controls() -> None:
    proof = _text("scripts/m5_uia_proof.ps1")
    for fragment in (
        "Репозиторій з поточного плану",
        "Повний шлях до локального Git-кореня",
        "Прив’язати локальний репозиторій",
        "Скасувати локальну прив’язку",
        "[System.Windows.Automation.ControlType]::ComboBox",
        "[System.Windows.Automation.ControlType]::Edit",
        "[System.Windows.Automation.ControlType]::Button",
        "Packaged local repository control was enabled before local Product Factory execution authority was configured.",
    ):
        assert fragment in proof

    wrapper = _text("scripts/v01_autostart_uia_proof.ps1")
    assert "m5_uia_proof.ps1" in wrapper

    workflow = _text(".github/workflows/m11-windows-release.yml")
    assert "tests/test_product_factory_local_repository_operator_ui.py" in workflow
