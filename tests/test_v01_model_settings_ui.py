from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_model_settings import V01ModelSettings
from scripts import nika_windows

ROOT = Path(__file__).resolve().parents[1]


def test_model_settings_form_is_semantic_and_action_registered() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    actions = (ROOT / "src/nika_core/kernel/default_actions.py").read_text(encoding="utf-8")
    assert '<section aria-labelledby="model-settings-heading">' in html
    assert '<label for="model-route-kind">Тип маршруту моделі</label>' in html
    for route_kind in (
        "deterministic",
        "foundry_local",
        "ollama",
        "openai_compatible",
    ):
        assert f'<option value="{route_kind}">' in html
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


def test_packaged_missing_model_selection_rejects_task_and_focuses_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "a.txt").write_text("alpha", encoding="utf-8")
    (source_root / "b.txt").write_text("beta", encoding="utf-8")
    config = AppConfig(
        database_path=(tmp_path / "nika.db").resolve(),
        v01_source_root=source_root.resolve(),
        v01_source_a=Path("a.txt"),
        v01_source_b=Path("b.txt"),
    )
    monkeypatch.setattr(
        nika_windows.DesktopBackend,
        "_schedule_start",
        lambda _self, _task_id, _command: None,
    )
    bridge, _products = nika_windows.build_windows_bridge(config)

    result = bridge.dispatch(
        {
            "request_id": "missing-model-mode",
            "action_id": "task.create",
            "payload": {"command": "Порівняй два контрольовані джерела."},
        }
    )

    assert result["status"] == "rejected"
    assert "виберіть режим" in result["message"]
    assert result["focus_id"] == "model-route-kind"
    assert bridge.get_state()["state"]["tasks"] == []

    configured = bridge.dispatch(
        {
            "request_id": "save-deterministic-mode",
            "action_id": "settings.model.configure",
            "payload": {
                "revision": 0,
                "route_kind": "deterministic",
                "provider_id": None,
                "model": None,
                "base_url": None,
                "credential_ref": None,
                "private_data_allowed": True,
                "timeout_seconds": 60,
            },
        }
    )
    assert configured["status"] == "completed"

    accepted = bridge.dispatch(
        {
            "request_id": "explicit-deterministic-mode",
            "action_id": "task.create",
            "payload": {"command": "Порівняй два контрольовані джерела."},
        }
    )
    assert accepted["status"] == "accepted"
    assert accepted["focus_id"] == "tasks-heading"
    tasks = bridge.get_state()["state"]["tasks"]
    assert len(tasks) == 1
    selection = V01ModelSettings(SQLiteStore(config.database_path)).for_task(
        tasks[0]["task_id"]
    )
    assert selection.route_kind == "deterministic"
    assert selection.provider_id is None
    assert selection.model is None
    assert selection.base_url is None
    assert selection.credential_ref is None


def test_model_settings_backend_focus_precedes_potentially_slow_state_refresh() -> None:
    app = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    start = app.index("  async function dispatchModel(")
    end = app.index("\n  function renderAutostart(", start)
    body = app[start:end]
    focus_attempt = "const focusApplied = focusId ? focusElementById(focusId) : false;"
    refresh = "await refreshState({ announceTeamTransitions: false })"
    assert focus_attempt in body
    assert body.index(focus_attempt) < body.index(refresh)
    assert "if (!focusApplied)" in body


def test_packaged_bridge_reuses_integrated_model_settings_and_freezes_task_choice() -> None:
    script = (ROOT / "scripts/nika_windows.py").read_text(encoding="utf-8")
    assert "from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings" in script
    assert "model_settings = V01ModelSettings(store)" in script
    assert "source_bound = source_settings.prepare_task_payload(payload)" in script
    assert 'if model_snapshot.get("status") == "missing":' not in script
    assert "model_snapshot = model_settings.snapshot()" not in script
    assert "return model_settings.prepare_task_payload(source_bound)" in script
    assert "except ModelSetupError as exc:" in script
    assert 'focus_id="model-route-kind"' in script
    assert "ordinary_handler=create_ordinary_task" in script
    assert 'state["v01_model_settings"] = model_settings.snapshot()' in script
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


def test_packaged_uia_proof_covers_model_controls_and_selected_model_transport() -> None:
    proof = (ROOT / "scripts/m5_uia_proof.ps1").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts/v01_autostart_uia_proof.ps1").read_text(encoding="utf-8")
    assert "'Модель для нових завдань'" in proof
    assert "'Тип маршруту моделі'" in proof
    assert "'Назва моделі'" in proof
    assert "'Зберегти модель'" in proof
    assert "ControlType]::ComboBox" in proof
    assert "Set-BoundControlValue $modelNameControl 'uia-proof-model'" in proof
    assert (
        "Wait-BoundTextEvidence 'Модель збережено для нових завдань: "
        "ollama, uia-proof-model.'"
        in proof
    )
    assert "Wait-BoundTextEvidence 'Модель збережено для нових завдань.'" not in proof
    assert "v01_model_selection" in proof
    assert "v01_model_selections" in proof
    assert "hashlib.sha256(body.encode('utf-8')).hexdigest() != selection_id" in proof
    assert "'credential_ref': None" in proof

    assert "ThreadingHTTPServer" in wrapper
    assert '("127.0.0.1", 11434)' in wrapper
    assert "'/api/chat'" in wrapper
    assert "'uia-proof-model'" in wrapper
    assert "$lines.Count -ne 3" in wrapper
    assert "$request.stream -ne $false" in wrapper
    assert "$request.think -ne $false" in wrapper
    assert "$request.authorization_present -ne $false" in wrapper
    assert "Physical Ollama/model inference remains unverified." in wrapper

    invocation = "-WindowTitle $WindowTitle -VerifySourceSetup"
    first_generic = wrapper.index(invocation)
    retry_generic = wrapper.index(invocation, first_generic + len(invocation))
    reset_log = wrapper.index(
        "Remove-Item -LiteralPath $requestLog -Force -ErrorAction SilentlyContinue",
        first_generic,
    )
    transport_assertion = wrapper.index("Assert-SelectedModelRequests", retry_generic)
    first_enable = wrapper.index("-AutostartPhase Enable", transport_assertion)
    assert first_generic < reset_log < retry_generic < transport_assertion < first_enable
