from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_model_settings_form_is_semantic_and_action_registered() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    actions = (ROOT / "src/nika_core/kernel/default_actions.py").read_text(encoding="utf-8")
    assert '<section aria-labelledby="model-settings-heading">' in html
    assert '<label for="model-route-kind">Тип маршруту моделі</label>' in html
    assert '<label for="model-provider">Ідентифікатор постачальника</label>' in html
    assert '<label for="model-name">Назва моделі</label>' in html
    assert '<label for="model-base-url">Базова адреса постачальника</label>' in html
    assert '<label for="model-credential-ref">Посилання на змінну середовища API</label>' in html
    assert 'id="model-private-data" type="checkbox"' in html
    assert 'id="model-timeout" type="number"' in html
    assert 'data-action-id="settings.model.configure"' in html
    assert 'data-action-id="settings.model.refresh"' in html
    assert '"settings.model.configure"' in actions
    assert '"settings.model.refresh"' in actions
    assert html.count('aria-live="') == 1
    assert "API-ключ або пароль" in html
    assert '<section aria-labelledby="recovery-heading">' in html
    assert 'id="recovery-status"' in html
    assert 'aria-label="Стан відновлення після перезапуску"' in html
    for control_id in (
        "recovery-auto-count",
        "recovery-manual-count",
        "recovery-approval-count",
        "recovery-uncertain-count",
        "recovery-blocked-count",
        "recovery-failed-count",
    ):
        assert f'id="{control_id}"' in html


def test_packaged_bridge_reuses_integrated_model_settings_and_freezes_task_choice() -> None:
    script = (ROOT / "scripts/nika_windows.py").read_text(encoding="utf-8")
    assert "from nika_core.v01_model_settings import V01ModelSettings" in script
    assert "model_settings = V01ModelSettings(store)" in script
    assert "source_bound = source_settings.prepare_task_payload(payload)" in script
    assert "return model_settings.prepare_task_payload(source_bound)" in script
    assert '"v01_model_settings": model_settings.snapshot()' in script
    assert '"settings.model.configure": model_settings.configure' in script
    assert '"settings.model.refresh": refresh_model_settings' in script
    assert "V01BoundModelRuntimeFactory" not in script
    assert "backend.start_startup_recovery()" in script
    assert script.index("backend.start_startup_recovery()") < script.index(
        "products = ProductProjectCommandService"
    )
    assert script.index("backend.start_startup_recovery()") < script.index(
        "launch_windows_shell(bridge"
    )


def test_actual_renderer_model_settings_accessibility_races_and_secret_boundary() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the actual UI JavaScript")
    result = subprocess.run(
        [
            node,
            str(ROOT / "tests/js/model_settings_harness.cjs"),
            str(ROOT / "src/nika_core/ui/web/app.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: model settings + startup recovery renderer" in result.stdout


def test_packaged_uia_proof_covers_model_controls_and_durable_task_selection() -> None:
    proof = (ROOT / "scripts/m5_uia_proof.ps1").read_text(encoding="utf-8")
    assert "'Модель для нових завдань'" in proof
    assert "'Тип маршруту моделі'" in proof
    assert "'Назва моделі'" in proof
    assert "'Зберегти модель'" in proof
    assert "ControlType]::ComboBox" in proof
    assert "Set-BoundControlValue $modelNameControl 'uia-proof-model'" in proof
    assert "v01_model_selection" in proof
    assert "v01_model_selections" in proof
    assert "hashlib.sha256(body.encode('utf-8')).hexdigest() != selection_id" in proof
    assert "'credential_ref': None" in proof
    assert "never contacts" in proof
