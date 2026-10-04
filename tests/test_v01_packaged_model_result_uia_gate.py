from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_model_result_is_proved_in_one_bound_uia_tree_before_db_credit() -> None:
    proof = (ROOT / "scripts/m5_uia_proof.ps1").read_text(encoding="utf-8")
    source_phase = proof.index("    if ($VerifySourceSetup) {")
    terminal = proof.index(
        "Wait-BoundTextEvidence 'Командне завдання завершено; "
        "збережені результати учасників доступні.'",
        source_phase,
    )
    sequence = proof.index("Wait-BoundTextSequence @(", terminal)
    expected = (
        "'Відповідь моделі',",
        "$expectedModelResult,",
        "'Постачальник моделі',",
        "'ollama',",
        "'Модель',",
        "'uia-proof-model'",
    )
    cursor = sequence
    for line in expected:
        cursor = proof.index(line, cursor) + len(line)
    db_credit = proof.index("$modelBindingProbe = @'", cursor)
    assert terminal < sequence < cursor < db_credit

    assert "function Wait-BoundTextSequence(" in proof
    assert "foreach ($searchRoot in (Get-BoundSearchRoots $currentWindow))" in proof
    assert "[System.Windows.Automation.TreeScope]::Descendants" in proof
    assert "if ($name -ceq $ExpectedSequence[$sequenceIndex])" in proof
    assert "did not appear in one bound search root" in proof
    assert "NIKA_UIA_MODEL_RESULT_CANARY" in proof
    assert "^NIKA_UIA_MODEL_RESULT_[0-9a-f]{32}$" in proof
    assert "Wait-BoundTextEvidence $expectedModelResult" not in proof
    assert "Wait-BoundTextEvidence 'ollama'" not in proof


def test_controlled_model_response_canary_is_bounded_and_restored() -> None:
    wrapper = (ROOT / "scripts/v01_autostart_uia_proof.ps1").read_text(
        encoding="utf-8"
    )
    assert "ThreadingHTTPServer" in wrapper
    assert '("127.0.0.1", 11434)' in wrapper
    assert (
        "$resultCanary = 'NIKA_UIA_MODEL_RESULT_' + "
        "[guid]::NewGuid().ToString('N')"
    ) in wrapper
    assert "RESULT_TEXT = sys.argv[3]" in wrapper
    assert (
        're.fullmatch(r"NIKA_UIA_MODEL_RESULT_[0-9a-f]{32}", RESULT_TEXT)'
        in wrapper
    )
    assert '"content": RESULT_TEXT' in wrapper
    assert "controlled loopback response" not in wrapper
    assert '"`"$resultCanary`""'.replace("\\", "") in wrapper
    assert "$lines.Count -ne 3" in wrapper
    assert "$request.think -ne $false" in wrapper
    assert "$request.stream -ne $false" in wrapper
    assert "$request.authorization_present -ne $false" in wrapper

    assign = wrapper.index("$env:NIKA_UIA_MODEL_RESULT_CANARY = $resultCanary")
    server = wrapper.index("$qaServer = Start-Process", assign)
    first = wrapper.index("-WindowTitle $WindowTitle -VerifySourceSetup", server)
    second = wrapper.index(
        "-WindowTitle $WindowTitle -VerifySourceSetup", first + 1
    )
    discard = wrapper.index(
        "Remove-Item -LiteralPath $requestLog -Force -ErrorAction SilentlyContinue",
        first,
    )
    count = wrapper.index("    Assert-SelectedModelRequests", second)
    mutate = wrapper.index("-AutostartPhase Enable", count)
    restore = wrapper.index("'NIKA_UIA_MODEL_RESULT_CANARY',", mutate)
    assert assign < server < first < discard < second < count < mutate < restore
    assert "$previousResultCanary," in wrapper[restore:]


def test_real_windows_workflows_bind_packaged_model_result_proof() -> None:
    proof = "./scripts/v01_autostart_uia_proof.ps1"
    m11 = (ROOT / ".github/workflows/m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    m12 = (ROOT / ".github/workflows/m12-prehuman-release-gate.yml").read_text(
        encoding="utf-8"
    )
    assert m11.count(proof) == 1
    assert m12.count(proof) == 2
    assert f"{proof} -ExePath $extractedExe" in m12
