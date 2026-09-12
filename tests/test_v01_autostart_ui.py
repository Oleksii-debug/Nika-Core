from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_semantic_autostart_setting_has_explicit_save_and_read_only_status() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    assert '<section aria-labelledby="autostart-heading">' in html
    assert '<label for="autostart-enabled">Запускати Nika разом із Windows</label>' in html
    assert 'id="autostart-enabled" type="checkbox"' in html
    assert 'aria-describedby="autostart-help autostart-status"' in html
    assert 'data-action-id="settings.autostart.configure"' in html
    assert 'data-action-id="settings.autostart.refresh"' in html
    assert html.count('aria-live="') == 1


def test_actual_renderer_autostart_success_error_read_races_and_keyboard_contracts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the actual UI JavaScript")
    result = subprocess.run(
        [
            node,
            str(ROOT / "tests/js/autostart_settings_harness.cjs"),
            str(ROOT / "src/nika_core/ui/web/app.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: autostart renderer" in result.stdout


def test_packaged_restart_proof_reuses_strict_m5_identity_and_refuses_user_registry() -> None:
    wrapper = (ROOT / "scripts/v01_autostart_uia_proof.ps1").read_text(encoding="utf-8")
    m5 = (ROOT / "scripts/m5_uia_proof.ps1").read_text(encoding="utf-8")
    assert "RUNNER_ENVIRONMENT -ne 'github-hosted'" in wrapper
    assert "RUNNER_ENVIRONMENT -ne 'github-hosted'" in m5
    assert wrapper.index("GetValueNames() -contains 'NikaCore'") < wrapper.index(
        "-AutostartPhase Enable"
    )
    assert "-AutostartPhase Enable -VerifySourceSetup" in wrapper
    assert "-AutostartPhase Observe" in wrapper
    assert "-AutostartPhase Disable" in wrapper
    assert "$current -ceq $expectedCommand" in wrapper
    assert "$key.DeleteValue('NikaCore', $false)" in wrapper
    assert "DeleteSubKey" not in wrapper
    assert "HKEY_LOCAL_MACHINE" not in wrapper
    assert "ControlType]::CheckBox" in m5 and "TogglePattern]::Pattern" in m5
    assert "Resolve-BoundControlIdentity $autostartControl" in m5
    assert "Wait-FocusName $autostartControl" in m5
    for workflow in ("m11-windows-release.yml", "m12-prehuman-release-gate.yml"):
        source = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
        assert "run: ./scripts/v01_autostart_uia_proof.ps1" in source
