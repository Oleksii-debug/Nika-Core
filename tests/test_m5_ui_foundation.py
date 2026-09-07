from __future__ import annotations

from pathlib import Path


def test_packaged_uia_gate_waits_for_bridge_readiness_before_hotkeys() -> None:
    proof = Path(__file__).parents[1] / "scripts" / "m5_uia_proof.ps1"
    script = proof.read_text(encoding="utf-8")
    ready_wait = script.index("Wait-BoundTextEvidence 'Nika Core готова до роботи.'")
    alt_hotkey = script.index("SendWait('%1')")
    command_hotkey = script.index("SendWait('^+p')")
    assert ready_wait < alt_hotkey < command_hotkey
    assert "keyboard/focus flow verified successfully" in script
