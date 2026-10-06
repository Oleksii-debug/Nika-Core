from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "scripts" / "m5_uia_proof.ps1"


def test_packaged_uia_proof_exercises_direct_intelligence_mode_commands() -> None:
    source = PROOF.read_text(encoding="utf-8")

    deterministic = "Set-BoundControlValue $commandControl 'режим інтелекту deterministic'"
    ollama = (
        "Set-BoundControlValue $commandControl "
        "'режим інтелекту ollama uia-proof-model http://localhost:11434'"
    )
    assert deterministic in source
    assert ollama in source
    assert source.index(deterministic) < source.index(ollama)
    assert source.count("[System.Windows.Forms.SendKeys]::SendWait('^n')") >= 4


def test_packaged_uia_mode_commands_prove_durable_route_without_task_creation() -> None:
    source = PROOF.read_text(encoding="utf-8")

    assert (
        "SELECT revision, selection_json FROM v01_model_settings WHERE singleton = 1"
        in source
    )
    assert "if revision != 3:" in source
    assert "'route_kind': 'ollama'" in source
    assert "'provider_id': 'ollama'" in source
    assert "'model': 'uia-proof-model'" in source
    assert "'base_url': 'http://localhost:11434'" in source
    assert "'private_data_allowed': False" in source
    assert "SELECT COUNT(*) FROM tasks" in source
    assert "if task_count != 0:" in source


def test_packaged_uia_mode_mutations_require_focus_acknowledgement() -> None:
    source = PROOF.read_text(encoding="utf-8")
    start = source.index(
        "Set-BoundControlValue $commandControl 'режим інтелекту deterministic'"
    )
    end = source.index("$sourceRootControl =", start)
    lane = source[start:end]

    assert lane.count("Set-BoundControlFocus $startControl") == 2
    assert lane.count("[System.Windows.Forms.SendKeys]::SendWait('^n')") == 2
    assert lane.count("Wait-FocusName $commandControl") == 2
    assert "do not retry either effect inside this process" in lane
