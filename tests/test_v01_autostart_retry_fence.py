from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "v01_autostart_uia_proof.ps1"
_M5_SCRIPT = _ROOT / "scripts" / "m5_uia_proof.ps1"


def test_generic_keyboard_source_proof_has_one_bounded_nonmutating_retry_before_autostart() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    generic = text.index("-WindowTitle $WindowTitle -VerifySourceSetup")
    first_enable = text.index("-AutostartPhase Enable")
    generic_block = text[generic:first_enable]
    invocation = "-WindowTitle $WindowTitle -VerifySourceSetup"

    assert generic < first_enable
    assert "-AutostartPhase" not in generic_block
    assert generic_block.count(invocation) == 2
    assert generic_block.count("if ($LASTEXITCODE -ne 0)") == 2
    assert "retrying once in a fresh process" in generic_block
    assert "failed after the single non-mutating retry" in generic_block
    assert "while (" not in generic_block
    assert "for (" not in generic_block
    assert "Start-Sleep" not in generic_block


def test_m5_autostart_phases_are_isolated_from_generic_keyboard_prelude() -> None:
    text = _M5_SCRIPT.read_text(encoding="utf-8")

    assert "if ($VerifySourceSetup -and $AutostartPhase -ne 'None')" in text
    assert "Source setup proof must run in the non-mutating generic UIA phase." in text

    generic_guard = text.index("if ($AutostartPhase -eq 'None')")
    autostart_block = text.index("if ($AutostartPhase -ne 'None')", generic_guard)
    generic_block = text[generic_guard:autostart_block]

    assert "SendWait('%1')" in generic_block
    assert "SendWait('^+p')" in generic_block
    assert "Wait-FocusName $tasksControl" in generic_block
    assert "Wait-FocusName $commandControl" in generic_block


def test_enable_retry_requires_exact_zero_mutation_os_evidence() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    first_enable = text.index("-AutostartPhase Enable")
    failure_guard = text.index("if ($LASTEXITCODE -ne 0)", first_enable)
    observe = text.index("-AutostartPhase Observe", failure_guard)
    block = text[failure_guard:observe]

    unchanged_guard = "$null -eq $afterFailedEnable"
    retry = "-AutostartPhase Enable"
    changed_guard = "elseif ($afterFailedEnable -ceq $expectedCommand)"

    assert "$afterFailedEnable" in block
    assert unchanged_guard in block
    assert changed_guard in block
    assert "unexpected OS registration" in block
    assert "-VerifySourceSetup" not in block
    assert block.count(retry) == 1
    assert block.index(unchanged_guard) < block.index(retry)
    assert "while (" not in block
    assert "for (" not in block
    assert "Start-Sleep" not in block


def test_disable_retry_requires_exact_zero_mutation_os_evidence() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    first_disable = text.index("-AutostartPhase Disable")
    failure_guard = text.index("if ($LASTEXITCODE -ne 0)", first_disable)
    success = text.index("Write-Host 'Generic keyboard/source proof plus autostart UI", failure_guard)
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
