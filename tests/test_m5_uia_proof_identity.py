from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "m5_uia_proof.ps1"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_m5_uia_proof_binds_top_level_window_to_launched_process() -> None:
    source = _source()

    assert "ProcessIdProperty" in source
    assert "[System.Windows.Automation.AndCondition]::new" in source
    assert "Find-ExactWindow" in source
    assert "Multiple Nika Core top-level UI Automation windows matched" in source
    assert "$root.FindFirst([System.Windows.Automation.TreeScope]::Children" not in source


def test_m5_uia_proof_re_resolves_window_without_weakening_semantic_gate() -> None:
    source = _source()

    assert source.count("Find-ExactWindow") >= 4
    assert "ElementNotAvailableException" in source
    assert "Start-Process -FilePath $ExePath -PassThru" in source
    assert "Stop-Process -Id $process.Id -Force" in source
    for required_name in (
        "Nika Core",
        "Що має зробити Nika?",
        "Створити завдання",
        "Клавіатура",
        "Nika Core готова до роботи.",
        "Завдання",
    ):
        assert required_name in source

    assert "SendKeys]::SendWait('%1')" in source
    assert "SendKeys]::SendWait('^+p')" in source
