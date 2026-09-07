from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "scripts" / "m5_uia_proof.ps1"


def _runtime_id_helper() -> str:
    text = PROOF.read_text(encoding="utf-8")
    start = text.index("function Get-ElementRuntimeId(")
    end = text.index("\nfunction Test-SameAutomationElement(", start)
    return text[start:end]


def test_runtime_id_remains_mandatory_after_bounded_webview_provider_retry() -> None:
    helper = _runtime_id_helper()

    assert "[ValidateRange(1, 40)][int]$Attempts = 20" in helper
    assert "[ValidateRange(10, 500)][int]$DelayMilliseconds = 100" in helper
    assert "for ($attempt = 1; $attempt -le $Attempts; $attempt++)" in helper
    assert "$runtimeId = $Element.GetRuntimeId()" in helper
    assert "$runtimeId.Length -gt 0" in helper
    assert "return [int[]]$runtimeId" in helper
    assert "Start-Sleep -Milliseconds $DelayMilliseconds" in helper
    assert "did not expose a RuntimeId after bounded retry" in helper

    # The resilience change must never authorize a weaker identity source.
    assert "NativeWindowHandle" not in helper
    assert "AutomationId" not in helper
    assert "BoundingRectangle" not in helper
    assert "Click" not in helper
    assert "coordinates" in helper


def test_runtime_id_retry_does_not_swallow_stale_element_identity() -> None:
    helper = _runtime_id_helper()

    # A stale provider generation must still escape as ElementNotAvailable;
    # it is not converted into a name-only rebind.
    assert "catch [System.Windows.Automation.ElementNotAvailableException]" in helper
    assert "throw" in helper


def test_runtime_id_unavailable_retries_fresh_lookup_only_before_authority_capture() -> None:
    text = PROOF.read_text(encoding="utf-8")

    assert "class NikaUiaRuntimeIdUnavailableException" in text
    assert "throw [NikaUiaRuntimeIdUnavailableException]::new(" in text

    wait_start = text.index("function Wait-DescendantName(")
    wait_end = text.index("\n    function Wait-FocusName(", wait_start)
    wait_body = text[wait_start:wait_end]
    assert "catch [NikaUiaRuntimeIdUnavailableException]" in wait_body
    assert "retry the whole semantic lookup from fresh roots" in wait_body

    resolve_start = text.index("function Resolve-BoundControlIdentity(")
    resolve_end = text.index("\n    $window = $null", resolve_start)
    resolve_body = text[resolve_start:resolve_end]
    assert "catch [NikaUiaRuntimeIdUnavailableException]" not in resolve_body
    assert "$originalRuntimeId = Get-ElementRuntimeId $Identity.Element" in resolve_body
    assert "$resolvedRuntimeId = Get-ElementRuntimeId $resolved" in resolve_body


def test_autostart_observe_is_read_only_but_mutations_remain_generation_bound() -> None:
    text = PROOF.read_text(encoding="utf-8")

    assert "if ($AutostartPhase -ne 'Observe') {" in text
    assert "$target = if ($AutostartPhase -eq 'Observe')" in text
    assert "$autostartControl.Element" in text
    assert "Resolve-BoundControlIdentity $autostartControl" in text
    assert "$freshReadOnlyControl = Wait-DescendantName 'Запускати Nika разом із Windows'" in text
    assert "$target = $freshReadOnlyControl.Element" in text

    mutation_start = text.index("if ($AutostartPhase -ne 'Observe') {", text.index("$initialToggle"))
    mutation_end = text.index("$expectedStateText =", mutation_start)
    mutation_body = text[mutation_start:mutation_end]
    assert "Set-BoundControlFocus $autostartControl" in mutation_body
    assert "Set-BoundControlFocus $autostartSaveControl" in mutation_body
    assert "[System.Windows.Forms.SendKeys]::SendWait(' ')" in mutation_body


def test_read_only_text_evidence_allows_equivalent_uia_duplicates_without_action_authority() -> None:
    text = PROOF.read_text(encoding="utf-8")

    start = text.index("function Wait-BoundTextEvidence(")
    end = text.index("\n    function Wait-DescendantName(", start)
    helper = text[start:end]

    assert "ControlType]::Text" in helper
    assert "$matches.Count -gt 0" in helper
    assert "Get-BoundSearchRoots $currentWindow" in helper
    assert "New-BoundControlIdentity" not in helper
    assert "Resolve-BoundControlIdentity" not in helper
    assert "SetFocus" not in helper
    assert "SendKeys" not in helper

    assert "Wait-BoundTextEvidence $expectedStateText" in text
    assert "Wait-BoundTextEvidence 'Джерела збережено." in text
    assert "Wait-BoundTextEvidence 'Командне завдання завершено;" in text

    # Interactive and focusable controls still use unique bound identities.
    assert "Wait-DescendantName 'Запускати Nika разом із Windows'" in text
    assert "Wait-DescendantName 'Зберегти автозапуск'" in text
    assert "Wait-DescendantName 'Що має зробити Nika?'" in text
