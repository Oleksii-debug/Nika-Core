from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ENTRY = ROOT / "scripts" / "nika_windows.py"
JOURNEY = ROOT / "src" / "nika_core" / "product_factory_packaged_journey.py"
HTML = ROOT / "src" / "nika_core" / "ui" / "web" / "index.html"
UIA_PROOF = ROOT / "scripts" / "m5_uia_proof.ps1"


def test_packaged_command_help_preserves_intelligence_and_targeted_task_controls() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert (
        'aria-describedby="execution-mode command-intelligence-help task-control-help"'
        in html
    )
    assert 'id="command-intelligence-help"' in html
    assert 'id="task-control-help"' in html
    assert "режим інтелекту deterministic" in html
    assert "task status &lt;task_id&gt;" in html


def test_packaged_composition_keeps_both_command_authorities() -> None:
    entry = WINDOWS_ENTRY.read_text(encoding="utf-8")
    journey = JOURNEY.read_text(encoding="utf-8")

    assert "PackagedIntelligenceModeCommandAdapter" in entry
    assert "intelligence_mode_handler=intelligence_mode_commands.execute" in entry
    assert "is_packaged_intelligence_mode_command(command)" in journey
    assert "packaged_task_direct_target(command)" in journey
    assert 'return handler({"task_id": task_id} if task_id is not None else {})' in journey


def test_packaged_uia_proof_keeps_intelligence_and_targeted_task_journeys() -> None:
    proof = UIA_PROOF.read_text(encoding="utf-8")

    intelligence = (
        "Set-BoundControlValue $commandControl "
        "'режим інтелекту deterministic'"
    )
    targeted = 'Set-BoundControlValue $commandControl "task status $taskId"'
    source = "$sourceRootControl ="

    assert intelligence in proof
    assert targeted in proof
    assert source in proof
    assert proof.index(intelligence) < proof.index(source) < proof.index(targeted)
    assert "if task_count != 0:" in proof
    assert "if len(rows) != 1:" in proof
