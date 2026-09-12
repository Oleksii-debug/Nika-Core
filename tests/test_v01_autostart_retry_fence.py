from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "v01_autostart_uia_proof.ps1"


def test_enable_retry_requires_exact_zero_mutation_os_evidence() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    first_enable = text.index("-AutostartPhase Enable -VerifySourceSetup")
    failure_guard = text.index("if ($LASTEXITCODE -ne 0)", first_enable)
    observe = text.index("-AutostartPhase Observe", failure_guard)
    block = text[failure_guard:observe]

    unchanged_guard = "$null -eq $afterFailedEnable"
    retry = "-AutostartPhase Enable -VerifySourceSetup"
    changed_guard = "elseif ($afterFailedEnable -ceq $expectedCommand)"

    assert "$afterFailedEnable" in block
    assert unchanged_guard in block
    assert changed_guard in block
    assert "unexpected OS registration" in block
    assert block.count(retry) == 1
    assert block.index(unchanged_guard) < block.index(retry)
    assert "while (" not in block
    assert "for (" not in block
    assert "Start-Sleep" not in block


def test_disable_retry_requires_exact_zero_mutation_os_evidence() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    first_disable = text.index("-AutostartPhase Disable")
    failure_guard = text.index("if ($LASTEXITCODE -ne 0)", first_disable)
    success = text.index("Write-Host 'Autostart UI -> registration", failure_guard)
    block = text[failure_guard:success]

    unchanged_guard = "$afterFailedDisable -ceq $expectedCommand"
    retry = "-AutostartPhase Disable"
    changed_guard = "elseif ($null -eq $afterFailedDisable)"

    assert "$afterFailedDisable" in block
    assert unchanged_guard in block
    assert changed_guard in block
    assert "unexpected OS registration" in block
    assert block.count(retry) == 1
    assert block.index(unchanged_guard) < block.index(retry)
    assert "while (" not in block
    assert "for (" not in block
    assert "Start-Sleep" not in block
